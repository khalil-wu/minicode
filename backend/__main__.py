if __name__ == "__main__":
    import uvicorn
    # 提供默认启动方式，在开发调试时有帮助
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
