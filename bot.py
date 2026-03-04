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
REDIS_TOKEN = "ARAfAAImcDI2N2I1NDdkMjBkYTE0OWM3YmNjYzg1YWNhMjUxOWE2YXAyNDEyNw"

ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
executor = ThreadPoolExecutor(max_workers=8)  # 增加并发能力


# --- 2. 核心工具函数 ---

def redis_call(command, key, value=None, ex=2592000):
    if not REDIS_TOKEN: return None
    headers = {"Authorization": f"Bearer {REDIS_TOKEN}"}
    try:
        url = f"{REDIS_URL}/{command}/{key}"
        if command == "set":
            url += f"?ex={ex}"
            res = requests.post(url, headers=headers, data=json.dumps(value, ensure_ascii=False).encode('utf-8'),
                                timeout=5)
        else:
            res = requests.get(url, headers=headers, timeout=5)
        return res.json().get("result")
    except Exception as e:
        print(f"❌ Redis异常: {e}");
        return None


def search_tavily(query):
    if not TAVILY_KEY: return "搜索未配置。"
    try:
        url = "https://api.tavily.com/search"
        payload = {"api_key": TAVILY_KEY, "query": query, "search_depth": "smart", "include_answer": True}
        res = requests.post(url, json=payload, timeout=15)
        data = res.json()
        return data.get("answer") or "\n".join([r['content'] for r in data.get("results", [])[:3]])
    except Exception as e:
        print(f"🔍 搜索报错: {e}");
        return "联网搜索超时。"


# --- 3. 任务扫描器 (支持 ONCE/DAILY/WEEKLY/MONTHLY) ---

def send_reminder_card(chat_id, content, tag="提醒"):
    card = {
        "header": {"title": {"tag": "plain_text", "content": f"⏰ {tag}"},
                   "template": "purple" if "周期" in tag else "blue"},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": f"**计划任务执行：**\n{content}"}},
                     {"tag": "hr"}]
    }
    request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(
        CreateMessageRequestBody.builder().receive_id(chat_id).content(json.dumps(card)).msg_type("interactive").build()
    ).build()
    lark_client.im.v1.message.create(request)


def task_scanner():
    while True:
        try:
            now_bj = datetime.utcnow() + timedelta(hours=8)
            current_slot = now_bj.strftime("%Y%m%d%H%M")
            modes = {"remind": "单次提醒", "daily": "周期(每日)", "weekly": "周期(每周)", "monthly": "周期(每月)"}
            for prefix, tag in modes.items():
                keys = redis_call("keys", f"{prefix}:*:{current_slot}")
                if keys:
                    for k in keys:
                        txt = redis_call("get", k)
                        if txt:
                            cid = k.split(":")[1]
                            send_reminder_card(cid, txt, tag=tag)
                            redis_call("del", k)
                            # 自动续期
                            next_dt = None
                            if prefix == "daily":
                                next_dt = now_bj + timedelta(days=1)
                            elif prefix == "weekly":
                                next_dt = now_bj + timedelta(weeks=1)
                            elif prefix == "monthly":
                                next_dt = now_bj + timedelta(days=30)
                            if next_dt:
                                redis_call("set", f"{prefix}:{cid}:{next_dt.strftime('%Y%m%d%H%M')}", txt)
        except:
            pass
        time.sleep(15)


# --- 4. 核心业务逻辑 (单次请求架构) ---

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

    now_bj = datetime.utcnow() + timedelta(hours=8)

    # --- 步骤 1: 预检索 (仅拉取待办列表，不走AI判断) ---
    current_reminders = ""
    remind_keys = []
    for p in ["remind", "daily", "weekly", "monthly"]:
        found = redis_call("keys", f"{p}:{chat_id}:*")
        if found: remind_keys.extend(found)
    if remind_keys and any(k in query for k in ["提醒", "计划", "什么时候", "今天"]):
        items = [f"• {k.split(':')[-1][8:12]} {redis_call('get', k)}" for k in sorted(remind_keys)[:5]]
        current_reminders = "\n".join(items)

    # --- 步骤 2: 联网评估 (不再调用第二次 DeepSeek) ---
    # 我们根据 query 特征简单判断是否需要搜索，节省一次 AI 调用
    search_info = ""
    if any(k in query for k in ["榜单", "排行", "最新", "天气", "新闻", "多少钱", "怎么放假"]):
        search_info = search_tavily(query)

    # --- 步骤 3: 构造并发送终极请求 ---
    hist_raw = redis_call("get", f"hist_{chat_id}")
    history = json.loads(hist_raw) if hist_raw else []

    sys_prompt = f"""你是智能助理 Allen Agent。北京时间: {now_bj.strftime('%Y-%m-%d %H:%M')}。

    [能力说明]
    1. 你可以设定各种周期的提醒。
    2. 你知道当前的放假调休情况（见参考信息）。

    [待办列表]
    {current_reminders if current_reminders else "暂无待办事项。"}

    [实时参考]
    {search_info if search_info else "无需外部搜索。"}

    [输出规范]
    若要设定提醒，必须在回复末尾添加指令：@@@TASK_模式:内容|HH:mm@@@
    模式选一：ONCE, DAILY, WEEKLY, MONTHLY。
    """

    messages = [{"role": "system", "content": sys_prompt}]
    messages.extend(history[-16:])  # 保留 8 轮对话
    messages.append({"role": "user", "content": query})

    res = ai_client.chat.completions.create(model="deepseek-chat", messages=messages, temperature=0.3)
    ans = res.choices[0].message.content

    # --- 步骤 4: 指令解析与状态更新 ---
    notice_md = ""
    for cmd, prefix in [("ONCE", "remind"), ("DAILY", "daily"), ("WEEKLY", "weekly"), ("MONTHLY", "monthly")]:
        match = re.search(rf"@@@TASK_{cmd}:(.*?)@@@", ans)
        if match:
            try:
                content, raw_time = [x.strip() for x in match.group(1).split("|")]
                time_clean = re.search(r"(\d{1,2}:\d{2})", raw_time).group(1)
                target_dt = datetime.strptime(f"{now_bj.strftime('%Y-%m-%d')} {time_clean}", "%Y-%m-%d %H:%M")
                if target_dt < now_bj: target_dt += timedelta(days=1)

                redis_call("set", f"{prefix}:{chat_id}:{target_dt.strftime('%Y%m%d%H%M')}", content)
                notice_md = f"✅ 已成功录入{cmd}提醒：{content} ({target_dt.strftime('%H:%M')})"
                ans = re.sub(rf"@@@TASK_{cmd}:(.*?)@@@", "", ans).strip()
            except:
                pass

    # --- 步骤 5: 飞书推送 ---
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": ans}}]
    if notice_md: elements.extend(
        [{"tag": "hr"}, {"tag": "note", "elements": [{"tag": "lark_md", "content": notice_md}]}])

    lark_client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(msg_id).request_body(
        ReplyMessageRequestBody.builder().content(json.dumps(
            {"header": {"title": {"tag": "plain_text", "content": "Allen Agent"}}, "elements": elements})).msg_type(
            "interactive").build()
    ).build())

    # --- 步骤 6: 记忆存储 ---
    history.append({"role": "user", "content": query});
    history.append({"role": "assistant", "content": ans})
    redis_call("set", f"hist_{chat_id}", history[-20:], ex=7200)


# --- 5. 启动程序 ---
def main():
    threading.Thread(target=task_scanner, daemon=True).start()
    handler = EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
        lambda d: executor.submit(process_message_async, d)).build()
    print("🚀 Allen Agent 3.0 (High Performance) 已就绪")
    WsClient(app_id=APP_ID, app_secret=APP_SECRET, event_handler=handler).start()


if __name__ == "__main__": main()