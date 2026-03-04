import os, json, time, re, requests, base64
from datetime import datetime
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.api.task.v2 import *
from lark_oapi.api.optical_char_recognition.v1 import *
from lark_oapi.ws.client import Client as WsClient
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from openai import OpenAI
from tavily import TavilyClient

# --- 1. 初始化 ---
APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
TAVILY_KEY = os.getenv("TAVILY_KEY")
REDIS_URL = "https://hip-cougar-3988.upstash.io"
REDIS_TOKEN = "AQ-UAAImcDJlMGE1MmE1MzVjNzU0NWU0YjUzN2I2MjliYjc1MWQwNnAyMzk4OA"

ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
tavily_client = TavilyClient(api_key=TAVILY_KEY)
lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

processed_msgs = set()


# --- 2. 存储模块 ---
def redis_call(command, key, value=None):
    url = f"{REDIS_URL}/{command}/{key}"
    headers = {"Authorization": f"Bearer {REDIS_TOKEN}"}
    try:
        if command == "set":
            res = requests.post(url, headers=headers, data=json.dumps(value), timeout=5)
        else:
            res = requests.get(url, headers=headers, timeout=5)
        return res.json().get("result")
    except Exception as e:
        print(f"Redis 连接异常: {e}")
        return None


# --- 3. 工具模块 ---
def recognize_image_text(image_key, message_id):
    """
    修正版 OCR：解决 GetMessageResourceResponse 属性引用问题
    """
    try:
        # 1. 构造下载请求
        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(image_key) \
            .type("image") \
            .build()

        # 2. 执行下载
        response = lark_client.im.v1.message_resource.get(request)

        # 3. 检查下载是否成功
        if not response.success():
            return f"[下载图片失败: {response.msg}]"

        # 4. 获取二进制流 (核心修正点：直接从 response.file 读取)
        img_binary = response.file.read()
        if not img_binary:
            return "[图片内容为空]"

        img_base64 = base64.b64encode(img_binary).decode('utf-8')

        # 5. 调用 OCR 识别
        ocr_req = BasicRecognizeImageRequest.builder() \
            .request_body(BasicRecognizeImageRequestBody.builder()
                          .image(img_base64)
                          .build()) \
            .build()

        ocr_resp = lark_client.optical_char_recognition.v1.image.basic_recognize(ocr_req)

        if ocr_resp.success():
            text_list = ocr_resp.data.text_list
            return "\n".join(text_list) if text_list else "[图片中未检测到文字]"
        else:
            return f"[OCR 识别失败: {ocr_resp.msg}]"

    except Exception as e:
        # 打印详细错误到控制台方便你调试
        print(f"OCR Debug Error: {e}")
        return f"[OCR 模块异常: {str(e)}]"


def manage_task(command_type, data):
    """根据源码校准：使用 timestamp 字段并确保为 int 类型"""
    try:
        if command_type == "CREATE":
            if "|" not in data: return "❌ 任务指令解析失败"
            summary, t_str = data.split("|")

            # 时间清洗：提取 HH:mm
            time_match = re.search(r"(\d{1,2}:\d{2})", t_str)
            if not time_match: return f"❌ 无法解析时间: {t_str}"

            clean_time = time_match.group(1)
            now = datetime.now()

            # 构造毫秒级时间戳 (int 类型)
            dt_str = f"{now.strftime('%Y-%m-%d')} {clean_time}"
            ts = int(time.mktime(time.strptime(dt_str, "%Y-%m-%d %H:%M"))) * 1000

            # 跨天处理
            if ts < time.time() * 1000:
                ts += 24 * 3600 * 1000

            # --- 严格按照你提供的源码构造 ---
            due_obj = Due.builder() \
                .timestamp(ts) \
                .is_all_day(False) \
                .build()

            task_obj = Task.builder() \
                .summary(f"⏰ Allen Agent 提醒：{summary}") \
                .due(due_obj) \
                .build()

            request = CreateTaskRequest.builder().request_body(task_obj).build()
            resp = lark_client.task.v2.task.create(request)

            return "✅ 已同步至飞书待办" if resp.success() else f"❌ 同步失败: {resp.msg}"

        # DELETE 逻辑保持不变...
        return "❌ 操作未识别"
    except Exception as e:
        return f"⚠️ 任务模块异常: {str(e)}"


# --- 4. AI 核心 ---
def get_ai_answer(chat_id, query, img_text=""):
    # 网页正文提取
    urls = re.findall(r'https?://\S+', query)
    web_content = ""
    if urls:
        try:
            res = requests.post("https://api.tavily.com/extract", json={"api_key": TAVILY_KEY, "urls": [urls[0]]},
                                timeout=8)
            web_content = res.json().get('results', [{}])[0].get('raw_content', "")[:2000]
        except:
            pass

    # 联网搜索
    search_data = ""
    try:
        search_res = tavily_client.search(query=query, max_results=3)
        search_data = "\n".join([r['content'] for r in search_res['results']])
    except:
        pass

    # 记忆读取
    hist_raw = redis_call("get", f"hist_{chat_id}")
    history = json.loads(hist_raw) if hist_raw else []

    system_prompt = f"你是飞书全能助手Allen Agent。当前北京时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}。\n" \
                    "【指令准则】：\n" \
                    "1. 创建任务必须严格使用格式：>>>TASK_CREATE:内容|HH:mm<<< \n" \
                    "2. 严禁在 HH:mm 中包含'明天'、'下午'等中文字符，请根据当前时间自行换算为24小时制数字。\n" \
                    "3. 如果用户说'明天14点'，你只需输出 14:00，脚本会自动处理日期。"

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-10:])
    messages.append({"role": "user",
                     "content": f"图片文字:{img_text}\n网页正文:{web_content}\n搜索资料:{search_data}\n当前问题:{query}"})

    res = ai_client.chat.completions.create(model="deepseek-chat", messages=messages, temperature=0.3)
    ans = res.choices[0].message.content

    # 记忆存储
    history.append({"role": "user", "content": query})
    history.append({"role": "assistant", "content": ans})
    redis_call("set", f"hist_{chat_id}", history[-10:])
    return ans


# --- 5. 事件分发 ---
def handle_message(data: P2ImMessageReceiveV1) -> None:
    msg_obj = data.event.message
    msg_id = msg_obj.message_id
    chat_id = msg_obj.chat_id
    # 自测点：message_type 属性名校准
    msg_type = msg_obj.message_type

    if redis_call("get", f"msg_{msg_id}"): return
    redis_call("set", f"msg_{msg_id}", "1")

    query, img_txt = "", ""
    try:
        content_json = json.loads(msg_obj.content)
        if msg_type == "text":
            query = content_json.get("text", "").strip()
        # elif msg_type == "image":
        #     img_txt = recognize_image_text(content_json.get("image_key"), msg_id)
        #     query = "识别图中文字并分析"
        elif msg_type == "image":
            try:
                content_json = json.loads(msg_obj.content)
                image_key = content_json.get("image_key")
                # 务必传入 message_id
                img_txt = recognize_image_text(image_key, msg_id)

                # 这里的 query 不能是空的，否则 AI 不会思考
                query = f"这是我发送的图片内容识别结果：\n{img_txt}\n请根据以上内容进行分析。"
            except Exception as e:
                query = f"识别图片时发生错误: {str(e)}"
    except:
        return

    if not query: return

    ans = get_ai_answer(chat_id, query, img_txt)

    # 指令处理
    notice = ""
    for pattern in [r">>>TASK_CREATE:(.*?)<<<", r">>>TASK_DELETE:(.*?)<<<"]:
        match = re.search(pattern, ans)
        if match:
            cmd = "CREATE" if "CREATE" in pattern else "DELETE"
            notice = manage_task(cmd, match.group(1))
            ans = ans.replace(match.group(0), "").strip()
            break

    # 自测点：卡片列表元素不可为 None
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": ans}}]
    if notice:
        elements.extend([{"tag": "hr"}, {"tag": "note", "elements": [{"tag": "lark_md", "content": notice}]}])

    card = {
        "header": {"title": {"tag": "plain_text", "content": "Allen Agent"}, "template": "indigo"},
        "elements": elements
    }

    reply_req = ReplyMessageRequest.builder() \
        .message_id(msg_id) \
        .request_body(ReplyMessageRequestBody.builder().content(json.dumps(card)).msg_type("interactive").build()) \
        .build()
    lark_client.im.v1.message.reply(reply_req)


# --- 6. 入口 ---
event_handler = EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(handle_message).build()


def main():
    print(f"[{datetime.now()}] 🚀 Allen Agent | 正在建立稳健连接...")

    # 建立客户端时可以指定日志级别
    ws_client = WsClient(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO  # 如果想看更细的连接细节，可以改用 DEBUG
    )

    # 这种启动方式会自动处理重连
    # 即使出现 keepalive ping timeout，SDK 也会在 3-5 秒内自动找回连接
    ws_client.start()


if __name__ == "__main__":
    main()