#!/usr/bin/env python3
"""
Google Calendar OAuth 설정 스크립트.

사전 준비:
1. https://console.cloud.google.com 에서 프로젝트 생성
2. Google Calendar API 활성화
3. OAuth 동의 화면 설정 (External, 테스트 사용자에 사용할 이메일 추가)
4. OAuth 2.0 클라이언트 ID 생성 (유형: 데스크톱 앱)
5. 이 스크립트를 캘린더별로 실행 (개인/회사 각 1회)

실행:
  python setup_calendar.py
"""

import json
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request

print("=== Google Calendar 연동 설정 ===\n")
print("어떤 캘린더를 설정할까요?")
print("  1) 개인 캘린더 (personal)")
print("  2) 회사 캘린더 (work)")
choice = input("\n선택 (1 또는 2): ").strip()

if choice == "2":
    cal_type = "work"
    cal_label = "회사"
else:
    cal_type = "personal"
    cal_label = "개인"

CLIENT_ID = input("\nGoogle Client ID: ").strip()
CLIENT_SECRET = input("Google Client Secret: ").strip()

SCOPE = "https://www.googleapis.com/auth/calendar"
REDIRECT_URI = "http://localhost:8090"

auth_url = (
    f"https://accounts.google.com/o/oauth2/v2/auth?"
    f"client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code"
    f"&scope={SCOPE}&access_type=offline&prompt=consent"
)

print(f"\n{cal_label} 캘린더용 구글 계정으로 로그인하세요...")
webbrowser.open(auth_url)

code = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global code
        query = parse_qs(urlparse(self.path).query)
        code = query.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"✅ {cal_label} 캘린더 인증 완료! 이 탭을 닫아도 됩니다.".encode("utf-8"))

    def log_message(self, format, *args):
        pass


server = HTTPServer(("localhost", 8090), Handler)
# 브라우저가 favicon 등 추가 요청을 보낼 수 있으므로 여러 번 시도
for _ in range(5):
    server.handle_request()
    if code:
        break

if not code:
    print("❌ 인증 코드를 받지 못했습니다.")
    exit(1)

data = (
    f"client_id={CLIENT_ID}&client_secret={CLIENT_SECRET}"
    f"&code={code}&grant_type=authorization_code&redirect_uri={REDIRECT_URI}"
).encode()

req = Request(
    "https://oauth2.googleapis.com/token",
    data=data,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
resp = json.loads(urlopen(req).read())

if "refresh_token" not in resp:
    print(f"❌ 토큰 발급 실패: {resp}")
    exit(1)

CAL_TYPE_UPPER = cal_type.upper()
print(f"\n{'='*55}")
print(f"✅ {cal_label} 캘린더 설정 완료!")
print(f"   아래 값을 Railway 환경변수에 추가하세요:")
print(f"{'='*55}")
print(f"GOOGLE_CLIENT_ID={CLIENT_ID}")
print(f"GOOGLE_CLIENT_SECRET={CLIENT_SECRET}")
print(f"GOOGLE_CAL_{CAL_TYPE_UPPER}_REFRESH_TOKEN={resp['refresh_token']}")
print(f"GOOGLE_CAL_{CAL_TYPE_UPPER}_ID=primary")
print(f"{'='*55}")

if cal_type == "personal":
    print(f"\n💡 회사 캘린더도 설정하려면 다시 실행하세요:")
    print(f"   python setup_calendar.py")
