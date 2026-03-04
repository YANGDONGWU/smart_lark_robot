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
executor = ThreadPoolExecutor(max_workers=10)


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
    if not TAVILY_KEY: return ""
    try:
        url = "https://api.tavily.com/search"
        # 使用 advanced 深度搜索，确保拿到真实榜单数据
        payload = {"api_key": TAVILY_KEY, "query": query, "search_depth": "advanced", "include_answer": True}
        res = requests.post(url, json=payload, timeout=20)
        data = res.json()
        return data.get("answer") or "\n".join([r['content'] for r in data.get("results", [])[:5]])
    except Exception as e:
        print(f"🔍 搜索报错: {e}");
        return ""


# --- 3. 任务扫描与推送 ---

def send_reminder_card(chat_id, content, tag="提醒"):
    card = {
        "header": {"title": {"tag": "plain_text", "content": f"⏰ {tag}"},
                   "template": "purple" if "周期" in tag else "blue"},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": f"**事项：**\n{content}"}}, {"tag": "hr"}]
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
                            # 自动续期逻辑
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


# --- 4. 核心业务逻辑 (语义预判与单请求融合) ---

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

    # --- 步骤 1: 智能语义预判 (不再死记硬背关键词) ---
    search_info = ""
    # 正则识别：时间敏感词、事实询问词、数据趋势词
    semantic_signals = r"今|昨|明|后|现在|最近|目前|本周|本月|排行|名单|谁|哪|多少|榜|比分|价格|汇率|放假|趋势|动态|前十"
    if re.search(semantic_signals, query):
        print(f"🧠 语义预判命中：执行深度搜索内容 -> {query}")
        search_info = search_tavily(query)

    # --- 步骤 2: 状态与上下文提取 ---
    current_reminders = ""
    remind_keys = []
    for p in ["remind", "daily", "weekly", "monthly"]:
        found = redis_call("keys", f"{p}:{chat_id}:*")
        if found: remind_keys.extend(found)
    if remind_keys and any(re.search(r"提醒|计划|待办|安排|什么时候", query) for _ in [0]):
        items = [f"• {k.split(':')[-1][8:12]} {redis_call('get', k)}" for k in sorted(remind_keys)[:8]]
        current_reminders = "\n".join(items)

    hist_raw = redis_call("get", f"hist_{chat_id}")
    history = json.loads(hist_raw) if hist_raw else []

    # --- 步骤 3: 构造强约束系统 Prompt ---
    sys_prompt = f"""你是 Allen Agent。北京时间: {now_bj.strftime('%Y-%m-%d %H:%M')}。

    [强制规则]
    1. 你必须基于[实时参考]的数据回答。严禁回答“我无法获取”或“建议你自己去查”。
    2. 如果参考信息包含榜单或数据，请清晰地罗列出来。
    3. 若涉及节假日，请根据参考信息中的调休安排给出准确日期。

    [当前状态]
    待办列表: {current_reminders if current_reminders else "空"}
    实时参考: {search_info if search_info else "当前为常识讨论，无需外部数据。"}

    [指令规范]
    设定提醒必带：@@@TASK_模式:内容|HH:mm@@@ (模式: ONCE, DAILY, WEEKLY, MONTHLY)
    """

    # --- 步骤 4: 执行单次 AI 请求 ---
    messages = [{"role": "system", "content": sys_prompt}]
    messages.extend(history[-12:])  # 保持 6 轮记忆
    messages.append({"role": "user", "content": query})

    res = ai_client.chat.completions.create(model="deepseek-chat", messages=messages, temperature=0.3)
    ans = res.choices[0].message.content

    # --- 步骤 5: 结果解析与推送 ---
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
                notice_md = f"✅ 已存入{cmd}任务：{content} ({target_dt.strftime('%H:%M')})"
                ans = re.sub(rf"@@@TASK_{cmd}:(.*?)@@@", "", ans).strip()
            except:
                pass

    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": ans}}]
    if notice_md: elements.extend(
        [{"tag": "hr"}, {"tag": "note", "elements": [{"tag": "lark_md", "content": notice_md}]}])

    lark_client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(msg_id).request_body(
        ReplyMessageRequestBody.builder().content(json.dumps(
            {"header": {"title": {"tag": "plain_text", "content": "Allen Agent"}}, "elements": elements})).msg_type(
            "interactive").build()
    ).build())

    # --- 步骤 6: 记忆持久化 ---
    history.append({"role": "user", "content": query})
    history.append({"role": "assistant", "content": ans})
    redis_call("set", f"hist_{chat_id}", history[-20:], ex=7200)


# --- 5. 启动 ---
def main():
    threading.Thread(target=task_scanner, daemon=True).start()
    handler = EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
        lambda d: executor.submit(process_message_async, d)).build()
    print("🚀 Allen Agent 3.1 (Semantic Engine) 启动成功")
    WsClient(app_id=APP_ID, app_secret=APP_SECRET, event_handler=handler).start()


if __name__ == "__main__": main()