import os, json, time, re, requests, threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.ws.client import Client as WsClient
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from openai import OpenAI

# --- 1. 配置加载 (从环境变量读取) ---
APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
REDIS_URL = "https://together-reindeer-4127.upstash.io"
REDIS_TOKEN = os.getenv("FEISHU_REDIS_TOKEN")

# 客户端初始化
ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
executor = ThreadPoolExecutor(max_workers=5)


# --- 2. 稳健版 Redis 存储 ---
def redis_call(command, key, value=None, ex=2592000):
    # 检查环境变量是否存在
    if not REDIS_TOKEN or not REDIS_URL:
        print("❌ 错误：REDIS_TOKEN 或 REDIS_URL 未设置!")
        return None

    headers = {"Authorization": f"Bearer {REDIS_TOKEN}"}
    try:
        if command == "set":
            url = f"{REDIS_URL}/set/{key}?ex={ex}"
            # 【重要修正】Upstash SET 命令如果是存字符串，需要确保是正确的 JSON 格式
            res = requests.post(url, headers=headers, data=json.dumps(value), timeout=5)
        elif command == "keys":
            url = f"{REDIS_URL}/keys/{key}"
            res = requests.get(url, headers=headers, timeout=5)
        else:
            url = f"{REDIS_URL}/{command}/{key}"
            res = requests.get(url, headers=headers, timeout=5)

        # 打印响应状态码协助排查
        if res.status_code != 200:
            print(f"❌ Redis API 报错: 状态码 {res.status_code}, 内容 {res.text}")
            return None

        return res.json().get("result")
    except Exception as e:
        print(f"❌ Redis 请求异常: {e}")
        return None


# --- 3. 消息推送 (修正 Builder 属性问题) ---
def send_reminder_card(chat_id, content, is_daily=False):
    title = "🔄 每日循环提醒" if is_daily else "⏰ 一次性定时提醒"
    card = {
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "purple" if is_daily else "blue"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**提醒时间到！**\n📌 事项：{content}"}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "lark_md",
                                          "content": f"💡 生成于北京时间: {(datetime.utcnow() + timedelta(hours=8)).strftime('%H:%M')}"}]}
        ]
    }

    request = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(CreateMessageRequestBody.builder()
                      .receive_id(chat_id)
                      .content(json.dumps(card))
                      .msg_type("interactive")
                      .build()) \
        .build()

    resp = lark_client.im.v1.message.create(request)
    if not resp.success():
        print(f"❌ 推送失败: {resp.msg}")
    else:
        print(f"✅ 成功推送事项: {content}")


# --- 4. 任务扫描线程 (GitHub Actions 防漏逻辑) ---
def task_scanner():
    print(f"[{datetime.now()}] ⏰ GitHub 任务扫描器启动...")
    processed_this_minute = set()
    last_min = ""

    while True:
        try:
            # 强制锁死北京时间
            now_bj = datetime.utcnow() + timedelta(hours=8)
            current_slot = now_bj.strftime("%Y%m%d%H%M")

            # 跨分钟清空缓存
            if current_slot != last_min:
                processed_this_minute.clear()
                last_min = current_slot

            for prefix in ["remind", "daily"]:
                keys = redis_call("keys", f"{prefix}:*:{current_slot}")
                if keys and isinstance(keys, list):
                    for k in keys:
                        if k in processed_this_minute: continue

                        txt = redis_call("get", k)
                        if txt:
                            cid = k.split(":")[1]
                            send_reminder_card(cid, txt, is_daily=(prefix == "daily"))

                            processed_this_minute.add(k)
                            redis_call("del", k)

                            if prefix == "daily":
                                next_slot = (now_bj + timedelta(days=1)).strftime("%Y%m%d%H%M")
                                redis_call("set", f"daily:{cid}:{next_slot}", txt)
                                print(f"♻️ 每日任务续期: {next_slot}")
        except Exception as e:
            print(f"扫描器运行异常: {e}")
        time.sleep(15)


# --- 5. 消息接收与 AI 解析 ---
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

    # 提示 AI 显式使用 HH:mm 格式
    now_bj = datetime.utcnow() + timedelta(hours=8)

    # 【核心优化 1】极其严厉的 Prompt
    prompt = f"""你是助手Allen Agent。现在北京时间{now_bj.strftime('%Y-%m-%d %H:%M')}。
        你的任务是帮助用户设定提醒。

        规则：
        1. 如果用户要求设定提醒，你必须在回答的【最后一行】精准附带指令。
        2. 一次性指令格式：@@@TASK_ONCE:事项|HH:mm@@@
        3. 每日指令格式：@@@TASK_DAILY:事项|HH:mm@@@
        4. 不要缩写事项，不要更改 @@@ 符号。

        示例回复：
        好的，没问题。
        @@@TASK_ONCE:睡觉|23:43@@@
        """

    res = ai_client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": query}],
        temperature=0.1
    )
    ans = res.choices[0].message.content

    # 【核心优化 2】改进正则匹配 (换成更独特的 @@@ 符号避免冲突)
    notice_md = ""
    # 注意这里正则改成了 @@@
    for pattern, prefix in [(r"@@@TASK_ONCE:(.*?)@@@", "remind"), (r"@@@TASK_DAILY:(.*?)@@@", "daily")]:
        match = re.search(pattern, ans)
        if match:
            try:
                raw_data = match.group(1)
                content, raw_time = raw_data.split("|")
                time_match = re.search(r"(\d{1,2}:\d{2})", raw_time)
                if not time_match: continue
                time_clean = time_match.group(1)

                target_dt = datetime.strptime(f"{now_bj.strftime('%Y-%m-%d')} {time_clean}", "%Y-%m-%d %H:%M")
                if target_dt < now_bj: target_dt += timedelta(days=1)

                slot = target_dt.strftime("%Y%m%d%H%M")

                # 存入 Redis 并打印日志以便在 GitHub Actions 观察
                success = redis_call("set", f"{prefix}:{chat_id}:{slot}", content)
                print(f"DEBUG: 写入Redis {'成功' if success else '失败'} | Key: {prefix}:{chat_id}:{slot}")

                notice_md = f"✅ 已为您预约北京时间 {target_dt.strftime('%H:%M')} 提醒：{content}"
                # 隐藏掉回复中的原始指令串，保持美观
                ans = re.sub(pattern, "", ans).strip()
            except Exception as e:
                print(f"❌ 解析指令异常: {e}")

    # 回复用户
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": ans}}]
    if notice_md:
        elements.extend([{"tag": "hr"}, {"tag": "note", "elements": [{"tag": "lark_md", "content": notice_md}]}])

    lark_client.im.v1.message.reply(ReplyMessageRequest.builder()
                                    .message_id(msg_id)
                                    .request_body(ReplyMessageRequestBody.builder().content(json.dumps(
        {"header": {"title": {"tag": "plain_text", "content": "Allen Agent"}}, "elements": elements})).msg_type(
        "interactive").build())
                                    .build())

    # 获取历史记录 (建议增加这部分)
    hist_raw = redis_call("get", f"hist_{chat_id}")
    history = json.loads(hist_raw) if hist_raw else []

    now_bj = datetime.utcnow() + timedelta(hours=8)
    prompt = f"你是助手Allen Agent。现在北京时间{now_bj.strftime('%Y-%m-%d %H:%M')}。"  # ...

    # 构造带记忆的消息列表
    messages = [{"role": "system", "content": prompt}]
    messages.extend(history[-5:])  # 取最近 5 条
    messages.append({"role": "user", "content": query})

    res = ai_client.chat.completions.create(model="deepseek-chat", messages=messages, temperature=0.1)
    ans = res.choices[0].message.content

    # ... 解析指令逻辑不变 ...

    # 存回记忆 (存 1 小时即可，避免占用 Redis 空间)
    history.append({"role": "user", "content": query})
    history.append({"role": "assistant", "content": ans})
    redis_call("set", f"hist_{chat_id}", history[-10:], ex=3600)


def handle_message(data: P2ImMessageReceiveV1) -> None:
    executor.submit(process_message_async, data)


def main():
    threading.Thread(target=task_scanner, daemon=True).start()
    event_handler = EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(handle_message).build()
    print(f"🚀 Allen Agent 在 GitHub 环境启动成功 (时区: 北京时间)")
    WsClient(app_id=APP_ID, app_secret=APP_SECRET, event_handler=event_handler).start()


if __name__ == "__main__":
    main()