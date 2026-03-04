import os, json
import lark_oapi as lark
# 这里的类名严格对应你从源码中找到的 P2ImMessageReceiveV1
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageRequest, ReplyMessageRequestBody
from openai import OpenAI
from tavily import TavilyClient

# 1. 在全局作用域初始化配置，确保所有函数都能访问
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
TAVILY_KEY = os.getenv("TAVILY_KEY")

# 初始化各个客户端
client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
tavily = TavilyClient(api_key=TAVILY_KEY)


# 2. 意图识别：判定是否需要搜索
def judge_need_search(query):
    try:
        check_res = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system",
                 "content": "你是一个意图判定专家。如果用户问题涉及实时新闻、事实查证、最新数据、天气股价等需要联网的信息，请回答'YES'，否则回答'NO'。"},
                {"role": "user", "content": query}
            ],
            max_tokens=5,
            temperature=0
        )
        return "YES" in check_res.choices[0].message.content.upper()
    except Exception as e:
        print(f"意 vision 判定出错: {e}")
        return False


# 3. 获取 AI 回答
def get_ai_answer(query):
    # 这里会自动寻找全局变量 client 和 tavily
    if judge_need_search(query):
        print(f"--- 触发联网搜索 ---")
        try:
            search_res = tavily.search(query=query, max_results=3)
            context = "\n".join([f"来源: {r['url']}\n内容: {r['content']}" for r in search_res['results']])
            final_prompt = f"请参考以下资料回答：{query}\n\n资料库：\n{context}"
        except Exception as e:
            print(f"搜索失败: {e}")
            final_prompt = query
    else:
        print(f"--- 直接对话 ---")
        final_prompt = query

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": final_prompt}]
    )
    return response.choices[0].message.content


# 4. 飞书消息处理
def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    # 解析消息内容
    content_str = data.event.message.content
    try:
        msg_json = json.loads(content_str)
        user_query = msg_json.get("text", "")
    except Exception:
        user_query = content_str

    if not user_query:
        return

    print(f"收到用户消息: {user_query}")
    ai_answer = get_ai_answer(user_query)

    # 构造回复
    reply_req = ReplyMessageRequest.builder() \
        .message_id(data.event.message.message_id) \
        .request_body(ReplyMessageRequestBody.builder()
                      .content(json.dumps({"text": ai_answer}))
                      .msg_type("text")
                      .build()) \
        .build()

    # 使用全局初始化的 lark_client
    lark_client.im.v1.message.reply(reply_req)


# 5. 初始化飞书客户端与事件处理器
lark_client = lark.Client.builder() \
    .app_id(FEISHU_APP_ID) \
    .app_secret(FEISHU_APP_SECRET) \
    .build()

event_handler = lark.EventDispatcher.builder("", "") \
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
    .build()


def main():
    print("AI 助理启动成功，WebSocket 监听中...")
    # 显式传入 ID 和 Secret
    ws_client = lark.WsClient(
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        event_handler,
        lark.LogLevel.INFO
    )
    ws_client.start()


if __name__ == "__main__":
    main()