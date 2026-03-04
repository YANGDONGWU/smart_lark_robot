import os, json, time, re, requests, base64
from datetime import datetime
import lark_oapi as lark
# 修正后的全量导入，确保模型(TaskDue)和事件(P2ImMessageReceiveV1)都能找到
from lark_oapi.api.im.v1 import *
from lark_oapi.api.task.v2 import *
from lark_oapi.api.optical_char_recognition.v1 import *
from lark_oapi.ws.client import Client as WsClient
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from openai import OpenAI
from tavily import TavilyClient

# --- 配置加载 ---
APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
TAVILY_KEY = os.getenv("TAVILY_KEY")

# Upstash Redis 配置
REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
tavily_client = TavilyClient(api_key=TAVILY_KEY)
lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()


# --- [核心] Redis 持久化存储工具 ---
def redis_call(command, key, value=None):
    """通过 REST API 操作 Upstash Redis"""
    url = f"{REDIS_URL}/{command}/{key}"
    headers = {"Authorization": f"Bearer {REDIS_TOKEN}"}
    try:
        if command == "set":
            res = requests.post(url, headers=headers, data=json.dumps(value))
        else:
            res = requests.get(url, headers=headers)
        return res.json().get("result")
    except:
        return None


def get_history(chat_id):
    data = redis_call("get", f"hist_{chat_id}")
    return json.loads(data) if data else []


def save_history(chat_id, history):
    # 只保留最近 10 条对话，防止 Redis 满载
    redis_call("set", f"hist_{chat_id}", history[-10:])


# --- [功能] OCR 识别 ---
def recognize_image_text(image_key):
    try:
        image_resp = lark_client.im.v1.message_resource.get().message_id("").file_key(image_key).type("image").build()
        request = BasicRecognizeImageRequest.builder() \
            .request_body(BasicRecognizeImageRequestBody.builder()
                          .image(base64.b64encode(image_resp.file.read()).decode('utf-8')).build()).build()
        response = lark_client.ocr.v1.image.basic_recognize(request)
        return "\n".join(response.data.text_list) if response.success() else "[OCR 失败]"
    except:
        return "[图片识别异常]"


# --- [功能] 任务管理 (同步飞书官方待办) ---
def manage_task(command_type, data):
    try:
        if command_type == "CREATE":
            summary, t_str = data.split("|")
            today = datetime.now().strftime("%Y-%m-%d")
            ts = int(time.mktime(time.strptime(f"{today} {t_str}", "%Y-%m-%d %H:%M"))) * 1000
            task = Task.builder().summary(f"⏰ 杨艾伦提醒：{summary}").due(
                Due.builder().time(str(ts)).timezone("Asia/Shanghai").build()).build()
            return "✅ 已同步至飞书待办" if lark_client.task.v2.task.create(
                CreateTaskRequest.builder().request_body(task).build()).success() else "❌ 创建失败"
        elif command_type == "DELETE":
            list_resp = lark_client.task.v2.task.list(ListTaskRequest.builder().completed(False).build())
            for t in list_resp.data.items if list_resp.success() else []:
                if data in t.summary:
                    lark_client.task.v2.task.delete(DeleteTaskRequest.builder().task_guid(t.task_guid).build())
                    return f"✅ 已取消含“{data}”的提醒"
        return "❌ 未找到匹配任务"
    except:
        return "⚠️ 任务操作异常"


# --- [核心] AI 逻辑 ---
def get_ai_answer(chat_id, query, img_text=""):
    # 提取网页
    urls = re.findall(r'https?://\S+', query)
    web_content = ""
    if urls:
        try:
            res = requests.post("https://api.tavily.com/extract", json={"api_key": TAVILY_KEY, "urls": [urls[0]]})
            web_content = res.json()['results'][0]['raw_content'][:2000]
        except:
            pass

    # 联网搜索
    search_data = ""
    try:
        search_data = "\n".join([r['content'] for r in tavily_client.search(query=query, max_results=3)['results']])
    except:
        pass

    # 获取 Redis 历史记录
    history = get_history(chat_id)

    system_prompt = f"你是飞书全能助手杨艾伦。当前北京时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}。\n" \
                    "指令格式：创建(>>>TASK_CREATE:内容|HH:mm<<<), 取消(>>>TASK_DELETE:关键词<<<)。"

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append(
        {"role": "user", "content": f"图片:{img_text}\n网页:{web_content}\n搜索:{search_data}\n问题:{query}"})

    res = ai_client.chat.completions.create(model="deepseek-chat", messages=messages)
    ans = res.choices[0].message.content

    # 存入 Redis 持久化记忆
    history.append({"role": "user", "content": query})
    history.append({"role": "assistant", "content": ans})
    save_history(chat_id, history)

    return ans


# --- 消息分发 ---
def handle_message(data: P2ImMessageReceiveV1) -> None:
    msg_id, chat_id, msg_type = data.event.message.message_id, data.event.message.chat_id, data.event.message.msg_type

    # 利用 Redis 进行消息 ID 去重，防止 GitHub 重启导致的重复回复
    if redis_call("get", f"msg_{msg_id}"): return
    redis_call("set", f"msg_{msg_id}", "1")

    query, img_txt = "", ""
    if msg_type == "text":
        query = json.loads(data.event.message.content).get("text", "").strip()
    elif msg_type == "image":
        img_txt = recognize_image_text(json.loads(data.event.message.content).get("image_key"))
        query = "识别图片文字并总结"

    if not query: return

    ans = get_ai_answer(chat_id, query, img_txt)

    # 指令解析
    notice = ""
    if ">>>TASK_CREATE:" in ans:
        notice = manage_task("CREATE", re.search(r">>>TASK_CREATE:(.*?)<<<", ans).group(1))
        ans = re.sub(r">>>TASK_CREATE:.*?<<<", "", ans)
    elif ">>>TASK_DELETE:" in ans:
        notice = manage_task("DELETE", re.search(r">>>TASK_DELETE:(.*?)<<<", ans).group(1))
        ans = re.sub(r">>>TASK_DELETE:.*?<<<", "", ans)

    card = {
        "header": {"title": {"tag": "plain_text", "content": "杨艾伦 Pro | 永不失忆版"}, "template": "indigo"},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": f"{ans}\n\n{notice}"}}]
    }
    lark_client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(msg_id).request_body(
        ReplyMessageRequestBody.builder().content(json.dumps(card)).msg_type("interactive").build()).build())


event_handler = EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(handle_message).build()


def main():
    WsClient(app_id=APP_ID, app_secret=APP_SECRET, event_handler=event_handler).start()


if __name__ == "__main__": main()