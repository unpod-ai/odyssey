import requests

headers = {
  "authorization": "639443b0263946fd80a721cb3d129a11"
}

response = requests.post(
    "https://llm-gateway.assemblyai.com/v1/chat/completions",
    headers = headers,
    json = {
        "model": "claude-opus-5",
        "messages": [
            {"role": "user", "content": "What is the capital of France?"}
        ],
        "max_tokens": 1000
    }
)

result = response.json()
print(result)
print(result["choices"][0]["message"]["content"])