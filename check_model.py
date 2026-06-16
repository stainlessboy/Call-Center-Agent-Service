# pip install langchain-openai
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    # model="Qwen/Qwen3.5-9B",
    # model="Qwen/Qwen3.5-397B-A17B",
    model="openai/gpt-oss-20b",
    base_url="https://api.together.xyz/v1",
    api_key="tgp_v1_N6XCtHCoKXrERqpmBAWz9ye__UCtHebbEJeZIPuFQYA",
    temperature=0,
)
result = llm.invoke('Сан узбек тилине биласанми ? ')
print(result.content)