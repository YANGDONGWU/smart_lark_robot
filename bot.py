import os, json, time
import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageRequest, ReplyMessageRequestBody
from lark_oapi.ws.client import Client as WsClient
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from openai import OpenAI
from tavily import TavilyClient

# 1. 配置与全局缓存
APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
TAVILY_KEY = os.getenv("TAVILY_KEY")

ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
tavily_client = TavilyClient(api_key=TAVILY_KEY)
lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

# 消息去重与多轮对话缓存
processed_msgs = set()
session_history = {}  # {chat_id: [{"role": "user", "content": "..."}, ...]}


def get_ai_answer(chat_id, user_query):
    # --- A. 每一条消息都强制联网搜索 ---
    print(f"--- 正在实时检索: {user_query} ---")
    search_context = ""
    try:
        # 搜索最近 3 年的数据，确保覆盖莱希遇难等突发事件
        search_res = tavily_client.search(query=user_query, search_depth="advanced", max_results=5)
        search_context = "\n".join([f"资料{i + 1}: {r['content']}" for i, r in enumerate(search_res['results'])])
    except Exception as e:
        print(f"搜索插件异常: {e}")

    # --- B. 构造多轮对话上下文 ---
    if chat_id not in session_history:
        session_history[chat_id] = []

    # 获取历史记录并限制长度（保留最近 6 条防止 Token 溢出）
    history = session_history[chat_id][-6:]

    # 构建当前任务的指令
    system_prompt = f"""
    你是飞书智能助手杨艾伦。当前实时时间：2026年3月4日。
    你拥有实时联网能力，必须基于下方提供的【最新搜索资料】回答。

    【核心事实校准】：
    - 伊朗前总统莱希已于2024年5月坠机身亡。
    - 现任伊朗总统是马蘇德·佩澤希齊揚（Masoud Pezeshkian）。

    如果用户指代不明（如问“他被杀了吗”），请结合【上下文历史】进行判断。
    """

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({
        "role": "user",
        "content": f"【最新搜索资料】：\n{search_context}\n\n【当前问题】：{user_query}"
    })

    # --- C. 调用 DeepSeek ---
    response = ai_client.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        temperature=0.3
    )
    answer = response.choices[0].message.content

    # 存入历史（不存搜索资料，只存对话）
    session_history[chat_id].append({"role": "user", "content": user_query})
    session_history[chat_id].append({"role": "assistant", "content": answer})

    return answer


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    msg_id = data.event.message.message_id
    chat_id = data.event.message.chat_id  # 用于区分不同人的对话

    # 1. 彻底解决重复回复：消息 ID 去重
    if msg_id in processed_msgs: return
    processed_msgs.add(msg_id)
    if len(processed_msgs) > 500: processed_msgs.clear()

    # 2. 解析消息内容
    try:
        content_dict = json.loads(data.event.message.content)
        user_query = content_dict.get("text", "").strip()
    except:
        return

    if not user_query: return

    # 3. 获取 AI 回答（带上下文）
    answer = get_ai_answer(chat_id, user_query)

    # 4. 回复飞书
    reply_req = ReplyMessageRequest.builder() \
        .message_id(msg_id) \
        .request_body(ReplyMessageRequestBody.builder()
                      .content(json.dumps({"text": answer}))
                      .msg_type("text")
                      .build()) \
        .build()
    lark_client.im.v1.message.reply(reply_req)


# --- 事件注册与启动 ---
event_handler = EventDispatcherHandler.builder("", "") \
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
    .build()


def main():
    print("杨艾伦已切换至【全量联网+多轮记忆】模式，监听中...")
    ws_client = WsClient(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO
    )
    ws_client.start()


if __name__ == "__main__":
    main()