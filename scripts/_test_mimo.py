"""Quick MiMo connectivity test."""
import sys
sys.path.insert(0, "src")

import httpx
from incomos.core.config import get_settings

s = get_settings()
print(f"Endpoint : {s.mimo_api_base_url}")
print(f"Model    : {s.mimo_model}")
print(f"Key      : {s.mimo_api_key[:10]}...")

payload = {
    "model": s.mimo_model,
    "messages": [{"role": "user", "content": 'Reply only with valid JSON: {"status":"ok"}'}],
    "max_tokens": 50,
}
headers = {
    "Authorization": f"Bearer {s.mimo_api_key}",
    "Content-Type": "application/json",
}
resp = httpx.post(
    f"{s.mimo_api_base_url}/chat/completions",
    json=payload,
    headers=headers,
    timeout=30,
)
print(f"HTTP     : {resp.status_code}")
data = resp.json()
content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
print(f"Response : {content[:300]}")
