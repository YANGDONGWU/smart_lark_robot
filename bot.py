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
REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

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
    """自测点：必须携带 message_id 下载资源，否则 403"""
    try:
        image_resp = lark_client.im.v1.message_resource.get() \
            .message_id(message_id) \
            .file_key(image_key) \
            .type("image") \
            .build()

        # 兼容性处理：读取二进制流
        img_data = image_resp.file.read()
        request = BasicRecognizeImageRequest.builder() \
            .request_body(BasicRecognizeImageRequestBody.builder()
                          .image(base64.b64encode(img_data).decode('utf-8'))
                          .build()) \
            .build()

        response = lark_client.optical_character_recognition.v1.image.basic_recognize(request)
        return "\n".join(response.data.text_list) if response.success() else "[OCR 失败]"
    except Exception as e:
        return f"[OCR 异常: {str(e)}]"


def manage_task(command_type, data):
    """自测点：修正 Task V2 模型对象名称"""
    try:
        if command_type == "CREATE":
            summary, t_str = data.split("|")
            # 处理时间戳
            today = datetime.now().strftime("%Y-%m-%d")
            ts = int(time.mktime(time.strptime(f"{today} {t_str}", "%Y-%m-%d %H:%M"))) * 1000

            # 使用 Due 模型而非 TaskDue
            due_obj = Due.builder().time(str(ts)).timezone("Asia/Shanghai").build()
            task_obj = Task.builder().summary(f"⏰ 提醒：{summary}").due(due_obj).build()

            req = CreateTaskRequest.builder().request_body(task_obj).build()
            resp = lark_client.task.v2.task.create(req)
            return "✅ 已同步至飞书待办" if resp.success() else f"❌ 创建失败: {resp.msg}"

        elif command_type == "DELETE":
            list_resp = lark_client.task.v2.task.list(ListTaskRequest.builder().completed(False).build())
            if list_resp.success() and list_resp.data.items:
                for t in list_resp.data.items:
                    if data in t.summary:
                        lark_client.task.v2.task.delete(DeleteTaskRequest.builder().task_guid(t.task_guid).build())
                        return f"✅ 已取消含“{data}”的提醒"
        return "❌ 未匹配到任务"
    except Exception as e:
        return f"⚠️ 任务异常: {str(e)}"


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

    system_prompt = f"你是飞书全能助手杨艾伦。当前北京时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}。\n" \
                    "指令格式：创建(>>>TASK_CREATE:内容|HH:mm<<<), 取消(>>>TASK_DELETE:关键词<<<)。"

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
        elif msg_type == "image":
            img_txt = recognize_image_text(content_json.get("image_key"), msg_id)
            query = "识别图中文字并分析"
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
        "header": {"title": {"tag": "plain_text", "content": "杨艾伦 Pro | 已自测"}, "template": "indigo"},
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
    print(f"[{datetime.now()}] 🚀 杨艾伦自测版启动成功，正在监听 WebSocket...")
    WsClient(app_id=APP_ID, app_secret=APP_SECRET, event_handler=event_handler).start()


if __name__ == "__main__":
    main()