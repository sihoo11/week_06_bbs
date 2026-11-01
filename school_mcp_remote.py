"""원격(HTTP) 배포용 MCP 서버.

폰 Claude 앱의 커넥터는 인증 헤더를 못 넣는 경우가 많아서,
추측할 수 없는 비밀 경로(MCP_SECRET)를 URL에 넣는 방식으로 접근을 막는다.
접속 주소: https://<배포주소>/<MCP_SECRET>/mcp
"""
import os

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

import school_mcp

SECRET = os.environ.get("MCP_SECRET", "")
if len(SECRET) < 16:
    raise SystemExit("환경변수 MCP_SECRET(16자 이상의 랜덤 문자열)을 설정하세요.")

# 배포 서비스의 도메인으로 들어오는 요청을 허용 (비밀 경로가 인증 역할)
security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

app = school_mcp.mcp.streamable_http_app(
    streamable_http_path=f"/{SECRET}/mcp",
    stateless_http=True,
    transport_security=security,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
