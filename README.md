# Wavelog ADI Auto Upload

Windows 局域网服务：通过 WebUI 管理多个人的 ADI 目录和 Wavelog API 配置，后台定时扫描并上传整份 `.adi` 文件。

## 启动

双击 `run_windows.bat`，首次运行会创建虚拟环境并安装依赖。浏览器打开 `http://电脑IP:10086/`。

需要 Windows 已安装 Python 3.10 或更高版本。启动脚本优先使用 Python Launcher（`py`），如果没有则自动尝试 `python` 命令。

如果启动失败，脚本会保留 CMD 窗口并显示具体原因；常见原因是 Python 未安装、未加入 PATH，或依赖下载失败。

服务监听 `0.0.0.0:10086`，当前版本不设置登录鉴权，请只在可信局域网使用，并在 Windows 防火墙中按需放行 10086 端口。

## Wavelog 配置

每个用户单独填写服务器 URL 和 API key。服务使用 Wavelog 官方 `/index.php/api/qso` JSON 接口，并自动通过 `/index.php/api/station_info/{key}` 选择启用的电台位置；一个 key 有多个位置时，可在界面填写明确的电台位置 ID。

SQLite 数据库位于 `data/wavelog.db`，包含用户配置和文件状态，重启后可恢复。

扫描器默认每 60 秒运行一次。按当前配置，每轮都会重新上传目录内的所有 ADI 文件，由 Wavelog 处理重复 QSO；同一文件正在排队、上传或等待失败重试时不会重复提交。失败会按 30 秒起步指数退避，自动尝试最多 5 次，之后可在页面手动重试。

## 开发测试

```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```
