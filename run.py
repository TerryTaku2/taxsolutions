"""Start the web app: python run.py  (then open http://127.0.0.1:8000)"""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("vatsys.web.app:create_app", factory=True, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")))
