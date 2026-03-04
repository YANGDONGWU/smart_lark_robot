import os, json, time, re, requests, threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.ws.client import Client as WsClient
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from openai import OpenAI

# --- 1. 配置加载 ---
APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
TAVILY_KEY = os.getenv("TAVILY_KEY")
REDIS_URL = "https://together-reindeer-4127.upstash.io"
# C部分暂不改动，保留你代码中的赋值方式
REDIS_TOKEN = "ARAfAAImcDI2N2I1NDdkMjBkYTE0OWM3YmNjYzg1YWNhMjUxOWE2YXAyNDEyNw"

ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
executor = ThreadPoolExecutor(max_workers=5)


# --- 2. 核心工具函数 ---

def redis_call(command, key, value=None, ex=2592000):
    if not REDIS_TOKEN: return None
    headers = {"Authorization": f"Bearer {REDIS_TOKEN}"}
    try:
        if command == "set":
            url = f"{REDIS_URL}/set/{key}?ex={ex}"
            res = requests.post(url, headers=headers, data=json.dumps(value, ensure_ascii=False).encode('utf-8'),
                                timeout=5)
        elif command == "keys":
            url = f"{REDIS_URL}/keys/{key}"
            res = requests.get(url, headers=headers, timeout=5)
        else:
            url = f"{REDIS_URL}/{command}/{key}"
            res = requests.get(url, headers=headers, timeout=5)
        return res.json().get("result")
    except Exception as e:
        print(f"❌ Redis异常: {e}")
        return None


def search_tavily(query):
    if not TAVILY_KEY: return ""
    try:
        url = "https://api.tavily.com/search"
        payload = {"api_key": TAVILY_KEY, "query": query, "search_depth": "smart", "include_answer": True}
        res = requests.post(url, json=payload, timeout=15)
        data = res.json()
        return data.get("answer") or "\n".join([r['content'] for r in data.get("results", [])[:3]])
    except Exception as e:
        print(f"🔍 搜索报错: {e}")
        return ""


# --- 3. 消息推送 ---

def send_reminder_card(chat_id, content, is_daily=False):
    if isinstance(content, str) and "\\u" in content:
        try:
            content = content.encode('utf-8').decode('unicode_escape')
        except:
            pass

    title = "🔄 每日循环提醒" if is_daily else "⏰ 一次性定时提醒"
    card = {
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "purple" if is_daily else "blue"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**提醒时间到！**\n📌 事项：{content}"}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "lark_md",
                                          "content": f"💡 计划执行于北京时间: {(datetime.utcnow() + timedelta(hours=8)).strftime('%H:%M')}"}]}
        ]
    }
    request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(
        CreateMessageRequestBody.builder().receive_id(chat_id).content(json.dumps(card)).msg_type("interactive").build()
    ).build()
    lark_client.im.v1.message.create(request)


# --- 4. 任务扫描线程 ---

def task_scanner():
    print(f"[{datetime.utcnow() + timedelta(hours=8)}] ⏰ 扫描器启动...")
    processed_keys = set()
    last_min = ""
    while True:
        try:
            now_bj = datetime.utcnow() + timedelta(hours=8)
            current_slot = now_bj.strftime("%Y%m%d%H%M")
            if current_slot != last_min:
                processed_keys.clear()
                last_min = current_slot

            for prefix in ["remind", "daily"]:
                keys = redis_call("keys", f"{prefix}:*:{current_slot}")
                if keys and isinstance(keys, list):
                    for k in keys:
                        if k in processed_keys: continue
                        txt = redis_call("get", k)
                        if txt:
                            cid = k.split(":")[1]
                            send_reminder_card(cid, txt, is_daily=(prefix == "daily"))
                            processed_keys.add(k)
                            redis_call("del", k)
                            if prefix == "daily":
                                next_slot = (now_bj + timedelta(days=1)).strftime("%Y%m%d%H%M")
                                redis_call("set", f"daily:{cid}:{next_slot}", txt)
        except Exception as e:
            print(f"扫描异常: {e}")
        time.sleep(15)


# --- 5. 核心业务逻辑 ---

def process_message_async(data: P2ImMessageReceiveV1):
    msg_obj = data.event.message
    chat_id, msg_id = msg_obj.chat_id, msg_obj.message_id

    if redis_call("get", f"msg_{msg_id}"): return
    redis_call("set", f"msg_{msg_id}", "1", ex=86400)

    try:
        query = json.loads(msg_obj.content).get("text", "").strip()
    except:
        return
    if not query: return

    # --- 逻辑优化：功能预检 (查询提醒/联网决策) ---

    current_reminders = ""
    # 场景A：用户查询已有提醒
    if any(k in query for k in ["我的提醒", "所有提醒", "查一下提醒", "有什么安排", "计划表"]):
        all_keys = []
        for p in ["remind", "daily"]:
            found = redis_call("keys", f"{p}:{chat_id}:*")
            if found: all_keys.extend(found)

        if all_keys:
            reminders_list = []
            for k in sorted(all_keys):
                val = redis_call("get", k)
                time_str = k.split(":")[-1]
                t_formatted = f"{time_str[8:10]}:{time_str[10:12]}"
                prefix_tag = " [每日]" if "daily" in k else ""
                reminders_list.append(f"• {t_formatted} {val}{prefix_tag}")
            current_reminders = "\n".join(reminders_list)
        else:
            current_reminders = "当前没有任何待办提醒。"

    # 场景B：联网搜索决策 (优化B：增加兜底处理)
    search_info = ""
    try:
        decision_res = ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": "判断是否需要联网获取最新数据。只需回答'搜'或'跳过'。"},
                      {"role": "user", "content": query}],
            temperature=0, max_tokens=5
        )
        if "搜" in decision_res.choices[0].message.content:
            search_info = search_tavily(query)
    except Exception as e:
        print(f"决策引擎故障: {e}")  # 故障时默认不搜索，直接进入下一步

    # --- 逻辑优化 A：记忆增强 (20条记录/10轮) ---
    hist_raw = redis_call("get", f"hist_{chat_id}")
    history = json.loads(hist_raw) if hist_raw else []

    now_bj = datetime.utcnow() + timedelta(hours=8)
    system_prompt = f"你是助手 Allen Agent。北京时间: {now_bj.strftime('%Y-%m-%d %H:%M')}。"

    if current_reminders:
        system_prompt += f"\n\n用户当前的待办提醒列表：\n{current_reminders}"
    if search_info:
        system_prompt += f"\n\n联网实时参考：\n{search_info}"

    system_prompt += "\n\n规则：设定提醒需附带指令：@@@TASK_ONCE:内容|HH:mm@@@ 或 @@@TASK_DAILY:内容|HH:mm@@@"

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-20:])  # 读 20 条
    messages.append({"role": "user", "content": query})

    # --- 生成回复 ---
    res = ai_client.chat.completions.create(model="deepseek-chat", messages=messages, temperature=0.2)
    ans = res.choices[0].message.content

    # --- 解析指令 ---
    notice_md = ""
    for pattern, prefix in [(r"@@@TASK_ONCE:(.*?)@@@", "remind"), (r"@@@TASK_DAILY:(.*?)@@@", "daily")]:
        match = re.search(pattern, ans)
        if match:
            try:
                raw_data = match.group(1)
                content, raw_time = [p.strip() for p in raw_data.split("|")]
                time_clean = re.search(r"(\d{1,2}:\d{2})", raw_time).group(1)
                target_dt = datetime.strptime(f"{now_bj.strftime('%Y-%m-%d')} {time_clean}", "%Y-%m-%d %H:%M")
                if target_dt < now_bj: target_dt += timedelta(days=1)
                slot = target_dt.strftime("%Y%m%d%H%M")
                redis_call("set", f"{prefix}:{chat_id}:{slot}", content)
                notice_md = f"✅ 已为您预约北京时间 {target_dt.strftime('%H:%M')} 提醒：{content}"
                ans = re.sub(pattern, "", ans).strip()
            except:
                pass

    # --- 推送 ---
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": ans}}]
    if notice_md: elements.extend(
        [{"tag": "hr"}, {"tag": "note", "elements": [{"tag": "lark_md", "content": notice_md}]}])

    lark_client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(msg_id).request_body(
        ReplyMessageRequestBody.builder().content(json.dumps(
            {"header": {"title": {"tag": "plain_text", "content": "Allen Agent"}}, "elements": elements})).msg_type(
            "interactive").build()
    ).build())

    # --- 记忆存储优化 A ---
    history.append({"role": "user", "content": query})
    history.append({"role": "assistant", "content": ans})
    redis_call("set", f"hist_{chat_id}", history[-30:], ex=7200)  # 存 30 条，有效期 2 小时


# --- 6. 运行 ---

def main():
    threading.Thread(target=task_scanner, daemon=True).start()
    handler = EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
        lambda d: executor.submit(process_message_async, d)).build()
    print(f"🚀 Allen Agent 2.1 强化版启动")
    WsClient(app_id=APP_ID, app_secret=APP_SECRET, event_handler=handler).start()


if __name__ == "__main__":
    main()