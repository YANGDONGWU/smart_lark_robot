import os, json, time, re, requests, base64, threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.ws.client import Client as WsClient
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from openai import OpenAI

# --- 1. 配置加载 (建议通过环境变量设置) ---
APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
REDIS_URL = "https://together-reindeer-4127.upstash.io"
REDIS_TOKEN = "ARAfAAImcDI2N2I1NDdkMjBkYTE0OWM3YmNjYzg1YWNhMjUxOWE2YXAyNDEyNw"

# 客户端初始化
ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
executor = ThreadPoolExecutor(max_workers=5)


# --- 2. 增强型 Redis 存储 (支持 TTL 过期机制) ---
def redis_call(command, key, value=None, ex=2592000):
    """
    默认 TTL 为 30 天 (2592000秒)，防止免费 Redis 打满
    """
    headers = {"Authorization": f"Bearer {REDIS_TOKEN}"}
    try:
        if command == "set":
            # Upstash REST API 支持通过 URL 参数设置过期时间
            url = f"{REDIS_URL}/set/{key}?ex={ex}"
            res = requests.post(url, headers=headers, data=json.dumps(value), timeout=5)
        elif command == "scan":
            url = f"{REDIS_URL}/keys/{key}"
            res = requests.get(url, headers=headers, timeout=5)
        else:
            url = f"{REDIS_URL}/{command}/{key}"
            res = requests.get(url, headers=headers, timeout=5)
        return res.json().get("result")
    except Exception as e:
        print(f"Redis 异常: {e}")
        return None


# --- 3. 定时任务执行模块 (主动推送) ---
def send_reminder_card(chat_id, content, is_daily=False):
    """
    当时间到达时，机器人主动在私聊中推送卡片
    """
    title = "🔄 每日循环提醒" if is_daily else "⏰ 一次性定时提醒"
    color = "purple" if is_daily else "blue"

    card = {
        "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**提醒时间到！**\n📌 事项：{content}"}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "lark_md", "content": "💡 来自 Allen Agent 的自动推送"}]}
        ]
    }

    request = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .receive_id(chat_id) \
        .content(json.dumps(card)) \
        .msg_type("interactive") \
        .build()
    lark_client.im.v1.message.create(request)


# --- 4. 任务扫描线程 (核心：支持每日续期) ---
def task_scanner():
    """
    后台线程：每 30 秒扫描一次 Redis，寻找匹配当前分钟的任务
    """
    print(f"[{datetime.now()}] ⏰ 任务扫描器已启动...")
    while True:
        try:
            now = datetime.now()
            slot = now.strftime("%Y%m%d%H%M")  # 格式：202603041430

            # 1. 处理一次性提醒 (remind:chat_id:slot)
            r_keys = redis_call("scan", f"remind:*:{slot}")
            if r_keys:
                for k in r_keys:
                    parts = k.split(":")
                    if len(parts) < 3: continue
                    cid, txt = parts[1], redis_call("get", k)
                    if txt:
                        send_reminder_card(cid, txt, is_daily=False)
                        redis_call("del", k)  # 发完即删

            # 2. 处理每日提醒 (daily:chat_id:slot)
            d_keys = redis_call("scan", f"daily:*:{slot}")
            if d_keys:
                for dk in d_keys:
                    parts = dk.split(":")
                    if len(parts) < 3: continue
                    cid, txt = parts[1], redis_call("get", dk)
                    if txt:
                        send_reminder_card(cid, txt, is_daily=True)
                        # --- 优雅改进：自动预约明天同一时间，并刷新30天过期时间 ---
                        next_day_slot = (now + timedelta(days=1)).strftime("%Y%m%d%H%M")
                        redis_call("set", f"daily:{cid}:{next_day_slot}", txt, ex=2592000)
                        redis_call("del", dk)
                        print(f"♻️ 每日任务续期成功: {txt} -> {next_day_slot}")
        except Exception as e:
            print(f"扫描器运行错误: {e}")
        time.sleep(30)


# --- 5. 消息处理与 AI 逻辑 ---
def process_message_async(data: P2ImMessageReceiveV1):
    msg_obj = data.event.message
    chat_id, msg_id = msg_obj.chat_id, msg_obj.message_id

    # 消息去重（存 24 小时）
    if redis_call("get", f"msg_{msg_id}"): return
    redis_call("set", f"msg_{msg_id}", "1", ex=86400)

    try:
        query = json.loads(msg_obj.content).get("text", "").strip()
    except:
        return
    if not query: return

    # AI 生成回答与指令
    prompt = f"你是飞书助手Allen Agent。当前时间{datetime.now().strftime('%Y-%m-%d %H:%M')}。\n" \
             "若要设提醒，必须在回复末尾精准包含指令：\n" \
             "1. 一次性：>>>TASK_ONCE:内容|HH:mm<<<\n" \
             "2. 每日循环：>>>TASK_DAILY:内容|HH:mm<<<"

    # 获取历史记忆 (存 7 天)
    hist_raw = redis_call("get", f"hist_{chat_id}")
    history = json.loads(hist_raw) if hist_raw else []

    messages = [{"role": "system", "content": prompt}]
    messages.extend(history[-6:])
    messages.append({"role": "user", "content": query})

    response = ai_client.chat.completions.create(model="deepseek-chat", messages=messages, temperature=0.3)
    ans = response.choices[0].message.content

    # 指令解析
    notice_md = ""
    for pattern, prefix in [(r">>>TASK_ONCE:(.*?)<<<", "remind"), (r">>>TASK_DAILY:(.*?)<<<", "daily")]:
        match = re.search(pattern, ans)
        if match:
            try:
                content, t_str = match.group(1).split("|")
                # 计算首次触发的 Slot
                target_dt = datetime.strptime(f"{datetime.now().strftime('%Y-%m-%d')} {t_str}", "%Y-%m-%d %H:%M")
                if target_dt < datetime.now(): target_dt += timedelta(days=1)

                slot = target_dt.strftime("%Y%m%d%H%M")
                redis_call("set", f"{prefix}:{chat_id}:{slot}", content, ex=2592000)

                type_name = "一次性" if prefix == "remind" else "每日"
                notice_md = f"✅ **{type_name}提醒设定成功**\n📌 事项：{content}\n⏰ 首次触发：{target_dt.strftime('%m-%d %H:%M')}"
                ans = ans.replace(match.group(0), "").strip()
            except:
                notice_md = "⚠️ 提醒解析失败，请检查时间格式。"

    # 存储记忆
    history.append({"role": "user", "content": query})
    history.append({"role": "assistant", "content": ans})
    redis_call("set", f"hist_{chat_id}", history[-10:], ex=604800)

    # 发送回复卡片
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": ans}}]
    if notice_md:
        elements.extend([{"tag": "hr"}, {"tag": "note", "elements": [{"tag": "lark_md", "content": notice_md}]}])

    lark_client.im.v1.message.reply(ReplyMessageRequest.builder()
                                    .message_id(msg_id)
                                    .request_body(ReplyMessageRequestBody.builder()
                                                  .content(
        json.dumps({"header": {"title": {"tag": "plain_text", "content": "Allen Agent"}}, "elements": elements}))
                                                  .msg_type("interactive").build()).build())


def handle_message(data: P2ImMessageReceiveV1) -> None:
    executor.submit(process_message_async, data)


# --- 6. 运行入口 ---
def main():
    # 启动定时任务扫描线程
    threading.Thread(target=task_scanner, daemon=True).start()

    # 建立 WebSocket 连接
    event_handler = EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(handle_message).build()
    print(f"[{datetime.now()}] 🚀 Allen Agent 个人版启动成功！")
    WsClient(app_id=APP_ID, app_secret=APP_SECRET, event_handler=event_handler).start()


if __name__ == "__main__":
    main()