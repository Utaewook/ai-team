"""소셜 로그인 흉내 사이트: 계속 움직이는 '카카오' 버튼 → 팝업 → (계속하기) → 쿠키 설정 후 팝업 닫힘."""
import http.server
import sys

AUTO = "--auto" in sys.argv     # 팝업이 '계속하기' 없이 바로 끝나는 경우


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, html, cookie=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(html.encode())

    def do_GET(self):
        logged = "logged=1" in (self.headers.get("Cookie") or "")
        if self.path.startswith("/home"):
            self.send("<p>홍길동님을 위한 추천</p>" if logged else "<a>로그인</a> <a>회원가입</a>")
        elif self.path.startswith("/auth"):
            self.send("<style>@keyframes w{from{transform:translateX(0)}to{transform:translateX(3px)}}"
                      ".b{animation:w .1s infinite alternate}</style>"
                      "<button class='b sns_kakao' onclick=\"window.open('/kakaoauth','p','width=400,height=500')\">"
                      "카카오 로그인</button>")
        elif self.path.startswith("/kakaoauth"):
            self.send("<script>location.href='/cb'</script>" if AUTO else
                      "<p>계정</p><button onclick=\"location.href='/cb'\">홍길동으로 계속하기</button>")
        elif self.path.startswith("/cb"):
            self.send("<script>window.opener&&window.opener.location.reload();window.close()</script>",
                      cookie="logged=1; Path=/")


http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
