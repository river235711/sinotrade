import urllib.request
import json
import os

token = "lvutV1BGReiUMJomqHoWRnWV6Q2vakTDyBg+uorbIT++el4AJQ/WXXAb+PRuEbJGAxbQgjVu5jEMza+aPSC/YQ5m5FZAPsgcaawQDlgyM/O2knCXXf3FEqcHPAp39HKmPJujO7Iq/DQKe9Cue9DtrAdB04t89/1O/w1cDnyilFU="
user_id = "Ueac4e9507728859429619c011f3851d2"

url = "https://api.line.me/v2/bot/message/push"
payload = {
    "to": user_id,
    "messages": [
        {
            "type": "text",
            "text": "🤖 [TMF 微台指當沖系統]\n\n✅ LINE 即時推播串接成功！\n\n📈 監控標的: 微型臺指期近月主力\n⚡ 訊號模式: 換分 0.2s 極速確認\n🛡️ 風控配置: ATR 動態停損 / 多階鎖利 / 不死鳥追平\n\n後續盤中所有進出場與結算將會第一時間發送到您的手機！"
        }
    ]
}

req = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
)

try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print("Status:", resp.status)
        print("Response:", resp.read().decode())
        print("LINE PUSH SUCCESSFUL!")
except urllib.error.HTTPError as e:
    print("HTTP Error:", e.code, e.read().decode())
except Exception as e:
    print("Error:", e)
