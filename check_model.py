# pip install langchain-openai
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="Qwen/Qwen3.5-9B",
    base_url="https://api.together.xyz/v1",
    api_key="",
    temperature=0,
)
result = llm.invoke('Сан узбек тилине биласанми ? ')
print(result)