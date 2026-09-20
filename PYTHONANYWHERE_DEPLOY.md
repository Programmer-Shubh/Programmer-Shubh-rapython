# PythonAnywhere Deployment Guide for RaTrade ALGO

## ⚠️ Important Limitation
PythonAnywhere natively supports **WSGI** (Flask/Django) only. FastAPI is **ASGI**. 
WebSockets and background tasks **won't work properly** on PythonAnywhere.

### Recommended Alternatives (Native ASGI Support)
| Platform | Free Tier | WebSockets | Background Tasks |
|----------|-----------|------------|------------------|
| **Railway** | $5/mo credit | ✅ | ✅ |
| **Render** | Free | ✅ | ✅ |
| **Fly.io** | Free allowance | ✅ | ✅ |
| **PythonAnywhere** | $5/mo | ❌ Limited | ❌ |

---

## PythonAnywhere Setup (if you must use it)

### 1. Create PythonAnywhere Account
- Go to https://www.pythonanywhere.com
- Sign up for "Beginner" ($5/mo) or "Hacker" ($12/mo) plan

### 2. Upload Code
```bash
# In PythonAnywhere Bash console:
git clone https://github.com/Programmer-Shubh/Programmer-Shubh-rapython.git
cd Programmer-Shubh-rapython
```

### 3. Install Dependencies
```bash
# In PythonAnywhere Bash console:
pip install -r requirements_pythonanywhere.txt
```

### 4. Configure Web App
1. Go to **Web** tab → **Add a new web app**
2. Choose **Manual configuration** (not Flask/Django)
3. Python version: **3.10** or **3.11**
3. Set source code path: `/home/yourusername/Programmer-Shubh-rapython`

### 5. Edit WSGI File
1. Click on **WSGI configuration file** link
2. Replace contents with:
```python
import os
import sys

project_root = '/home/yourusername/Programmer-Shubh-rapython'
sys.path.insert(0, project_root)

os.environ.setdefault("DB_PATH", os.path.join(project_root, "data", "ratrade.db"))

from fastapi.middleware.wsgi import WSGIMiddleware
from main import app as fastapi_app

application = WSGIMiddleware(fastapi_app)
```

### 6. Static Files
In **Static files** section:
- URL: `/static/`
- Path: `/home/yourusername/Programmer-Shubh-rapython/static`

### 7. Reload
Click **Reload** button

---

## ⚠️ What Won't Work on PythonAnywhere
- **WebSocket connections** (`/api/ws/live`, `/api/ws/chain/...`)
- **Background tasks** (data refresher, auto-trade, keepalive)
- **Real-time updates** (live positions, live chart)
- **Auto-trade execution** (needs persistent background process)

---

## Better Deployment Options

### Railway (Recommended - Easiest)
```bash
# 1. Install Railway CLI
npm i -g @railway/cli

# 2. Login & deploy
railway login
railway init
railway up
```
- Auto-detects FastAPI, sets up PostgreSQL
- WebSockets work out of the box
- Background workers supported
- $5/mo free credit covers small apps

### Render (Already Working)
Your app is already deployed at: `https://ratrade-tjzd.onrender.com`

### Fly.io (Docker-based)
```bash
# 1. Install flyctl
# 2. Create fly.toml
# 3. fly deploy
```
- Free allowance includes 3 shared-cpu VMs
- Full ASGI + WebSocket support
- Persistent volumes for SQLite

### VPS (Full Control)
- DigitalOcean Droplet ($4/mo)
- Linode/Akamai ($5/mo)  
- Install Docker, run with docker-compose
- Full control, but you manage the server

---

## Quick Railway Deployment

1. **Push to GitHub** (already done)
2. **Connect Railway**: https://railway.app → New Project → Deploy from GitHub
3. **Add PostgreSQL**: Railway → New → Database → PostgreSQL
4. **Set Environment Variables**:
   ```
   DATABASE_URL=<auto-provided by Railway>
   DB_PATH=/app/data/ratrade.db
   SELF_URL=https://your-app.railway.app
   ```
5. **Deploy** - Railway auto-detects FastAPI + uvicorn

---

## Summary
| If you need... | Use... |
|----------------|--------|
| Real-time WebSockets, background tasks, auto-trade | **Railway** / **Render** / **Fly.io** |
| Simple hosting, no WebSockets, manual reloads OK | **PythonAnywhere** |
| Full control, cheapest long-term | **VPS (DigitalOcean/Linode)** |

**Recommendation**: Stay on Render or move to Railway. PythonAnywhere is not suitable for this ASGI application with WebSockets.