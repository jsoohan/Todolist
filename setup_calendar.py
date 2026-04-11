#!/usr/bin/env python3
"""
Google Calendar OAuth 설정 스크립트 (1회만 실행).

사전 준비:
1. https://console.cloud.google.com 에서 프로젝트 생성
2. Google Calendar API 활성화
3. OAuth 동의 화면 설정 (External, 테스트 사용자에 본인 이메일 추가)
4. OAuth 2.0 클라이언트 ID 생성 (유형: 데스크톱 앱)
5. 이 스크립트 실행 → 브라우저에서 구글 로그인 → Railway 환경변수 설정

실행:
  pip install httpx
  python setup_calendar.py
"""

import json
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request

CLIENT_ID = input("Google Client ID: ").strip()
CLIENT_SECRET = input("Google Client Secret: ").strip()

SCOPE = "https://www.googleapis.com/auth/calendar"
REDIRECT_URI = "http://localhost:8090"

auth_url = (
    f"https://accounts.google.com/o/oauth2/v2/auth?"
    f"client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code"
    f"&scope={SCOPE}&access_type=offline&prompt=consent"
)

print(f"\n브라우저에서 구글 로그인 후 권한을 허용하세요...")
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
        self.wfile.write("✅ 인증 완료! 이 탭을 닫아도 됩니다.".encode("utf-8"))

    def log_message(self, format, *args):
        pass


server = HTTPServer(("localhost", 8090), Handler)
server.handle_request()

if not code:
    print("❌ 인증 코드를 받지 못했습니다.")
    exit(1)

# 코드 → 토큰 교환
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

print(f"\n{'='*50}")
print("✅ 아래 값을 Railway 환경변수에 추가하세요:")
print(f"{'='*50}")
print(f"GOOGLE_CLIENT_ID={CLIENT_ID}")
print(f"GOOGLE_CLIENT_SECRET={CLIENT_SECRET}")
print(f"GOOGLE_REFRESH_TOKEN={resp['refresh_token']}")
print(f"GOOGLE_CALENDAR_ID=primary")
print(f"{'='*50}")
