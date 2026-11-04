"""一键启动：python run.py"""

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("FISHCONSOLE_PORT", "8000"))
    # 控制台编码在 Windows 下默认是 GBK，直接 print 中文会变乱码，统一用可移植写法
    print(f"FishTank console started: http://127.0.0.1:{port}")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, reload=False)
