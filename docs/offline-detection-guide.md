# 网络离线检测增强 - 实现指南

## 概述

在 WebSocket 连接断开时提供智能重连和用户提示，改善离线体验。

---

## 实现方案

### 修改位置: `frontend/src.v2/hooks/useWebSocket.ts`

**完整实现**:

```typescript
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'  // 或你使用的 toast 库

export function useWebSocket(url: string) {
  const [isOnline, setIsOnline] = useState(true)
  const [reconnectAttempts, setReconnectAttempts] = useState(0)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null)
  const offlineToastIdRef = useRef<string | number | null>(null)

  const connect = () => {
    try {
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => {
        setIsOnline(true)
        setReconnectAttempts(0)

        // 连接恢复提示
        if (offlineToastIdRef.current) {
          toast.dismiss(offlineToastIdRef.current)
          toast.success('Connected', { duration: 2000 })
          offlineToastIdRef.current = null
        }

        console.log('[WebSocket] Connected')
      }

      ws.onclose = (event) => {
        setIsOnline(false)
        console.warn('[WebSocket] Disconnected', event.code, event.reason)

        // 5 秒后显示离线提示（避免短暂断线时闪烁）
        setTimeout(() => {
          if (!isOnline && !offlineToastIdRef.current) {
            offlineToastIdRef.current = toast.warning(
              'Network disconnected. Reconnecting...',
              { duration: Infinity }  // 保持显示直到重连
            )
          }
        }, 5000)

        // 指数退避重连
        const attempt = reconnectAttempts + 1
        setReconnectAttempts(attempt)

        const delay = Math.min(
          1000 * Math.pow(2, attempt),  // 1s, 2s, 4s, 8s, ...
          30000  // 最多 30 秒
        )

        console.log(`[WebSocket] Reconnecting in ${delay}ms (attempt ${attempt})`)

        reconnectTimeoutRef.current = setTimeout(() => {
          connect()
        }, delay)
      }

      ws.onerror = (error) => {
        console.error('[WebSocket] Error', error)
      }

      ws.onmessage = (event) => {
        // 处理消息...
        try {
          const data = JSON.parse(event.data)
          // 你的消息处理逻辑
        } catch (e) {
          console.error('[WebSocket] Failed to parse message', e)
        }
      }

    } catch (error) {
      console.error('[WebSocket] Connection failed', error)
      setIsOnline(false)
    }
  }

  useEffect(() => {
    connect()

    // 清理函数
    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
      if (offlineToastIdRef.current) {
        toast.dismiss(offlineToastIdRef.current)
      }
    }
  }, [url])

  return {
    isOnline,
    reconnectAttempts,
    ws: wsRef.current,
  }
}
```

---

## 使用示例

### 在主应用中集成

```typescript
// frontend/src.v2/App.tsx

import { useWebSocket } from './hooks/useWebSocket'

export function App() {
  const { isOnline, reconnectAttempts } = useWebSocket(
    `ws://${window.location.host}/ws`
  )

  return (
    <div className="app">
      {/* 顶部状态栏 */}
      {!isOnline && (
        <div className="offline-banner">
          <AlertCircle size={16} />
          <span>
            Offline {reconnectAttempts > 0 && `(retry ${reconnectAttempts})`}
          </span>
        </div>
      )}

      {/* 主要内容 */}
      <MainContent />
    </div>
  )
}
```

### CSS 样式

```css
/* frontend/src.v2/App.css */

.offline-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  background: #f59e0b;
  color: white;
  font-size: 14px;
  font-weight: 500;
  position: sticky;
  top: 0;
  z-index: 1000;
  animation: slideDown 0.3s ease-out;
}

@keyframes slideDown {
  from {
    transform: translateY(-100%);
    opacity: 0;
  }
  to {
    transform: translateY(0);
    opacity: 1;
  }
}
```

---

## 高级功能

### 心跳检测（可选）

```typescript
// 添加到 useWebSocket hook

const heartbeatIntervalRef = useRef<NodeJS.Timeout | null>(null)
const lastPongRef = useRef<number>(Date.now())

ws.onopen = () => {
  // ... 现有代码

  // 开始心跳
  heartbeatIntervalRef.current = setInterval(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'ping' }))

      // 10 秒内没有 pong，认为连接已死
      if (Date.now() - lastPongRef.current > 10000) {
        console.warn('[WebSocket] Heartbeat timeout, closing connection')
        wsRef.current.close()
      }
    }
  }, 5000)  // 每 5 秒一次心跳
}

ws.onmessage = (event) => {
  const data = JSON.parse(event.data)
  
  if (data.type === 'pong') {
    lastPongRef.current = Date.now()
    return
  }
  
  // 处理其他消息...
}

// 清理时停止心跳
return () => {
  if (heartbeatIntervalRef.current) {
    clearInterval(heartbeatIntervalRef.current)
  }
  // ... 其他清理
}
```

---

## 后端支持（可选）

### Python WebSocket 心跳响应

```python
# backend/api/websocket.py

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    try:
        while True:
            data = await websocket.receive_json()
            
            # 响应心跳
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            
            # 处理其他消息...
            
    except WebSocketDisconnect:
        print("Client disconnected")
```

---

## 测试场景

### 手动测试
1. **短暂断网** - 关闭 Wi-Fi 5 秒 → 应显示"Reconnecting..."
2. **长时间断网** - 关闭 Wi-Fi 30 秒 → 应持续重连，指数退避
3. **恢复连接** - 重新连接 Wi-Fi → 应显示"Connected"并自动恢复

### 开发工具测试
```typescript
// 在浏览器控制台中模拟断线
const ws = document.querySelector('...')  // 找到你的 WebSocket 实例
ws.close()  // 手动关闭连接，观察重连行为
```

---

## 优先级评估

**影响**: Medium（改善 UX，避免用户困惑）  
**复杂度**: Low  
**工作量**: 1 小时

**建议**: 简单但有效的改进，值得在 v1.0 中实现。

---

## 集成清单

- [x] 创建 useWebSocket hook
- [ ] 添加指数退避重连
- [ ] 集成 toast 通知
- [ ] 添加离线横幅（可选）
- [ ] 实现心跳检测（可选）
- [ ] 后端心跳响应（可选）
- [ ] 测试断线场景

---

## 注意事项

1. **避免过度提示** - 5 秒延迟避免短暂断线时闪烁
2. **指数退避** - 避免在网络异常时过度请求
3. **最大延迟** - 30 秒上限，保持响应性
4. **清理资源** - 组件卸载时清理定时器和 WebSocket
5. **用户反馈** - 明确告知重连状态和次数
