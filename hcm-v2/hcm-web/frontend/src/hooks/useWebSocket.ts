import { useEffect, useRef, useState, useCallback } from 'react';

export interface WebSocketMessage {
  type: string;
  symbol: string;
  payload: Record<string, unknown>;
  timestamp: number;
}

interface UseWebSocketOptions {
  onMessage?: (msg: WebSocketMessage) => void;
  onConnect?: () => void;
  onDisconnect?: () => void;
  reconnectInterval?: number;
  maxRetries?: number;
}

interface UseWebSocketReturn {
  isConnected: boolean;
  lastMessage: WebSocketMessage | null;
  sendMessage: (msg: Partial<WebSocketMessage>) => void;
  reconnect: () => void;
}

const WS_BASE_URL: string = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`;

/**
 * WebSocket hook for real-time symbol signal streaming.
 * Handles auto-reconnect and message parsing.
 */
export function useWebSocket(
  symbol: string,
  options: UseWebSocketOptions = {},
): UseWebSocketReturn {
  const {
    onMessage,
    onConnect,
    onDisconnect,
    reconnectInterval = 3000,
    maxRetries = 10,
  } = options;

  const [isConnected, setIsConnected] = useState<boolean>(false);
  const [lastMessage, setLastMessage] = useState<WebSocketMessage | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const retriesRef = useRef<number>(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    try {
      const token: string | null = localStorage.getItem('hcm_token');
      const wsUrl: string = `${WS_BASE_URL}?symbol=${encodeURIComponent(symbol)}${token ? `&token=${token}` : ''}`;
      const ws: WebSocket = new WebSocket(wsUrl);

      ws.onopen = (): void => {
        setIsConnected(true);
        retriesRef.current = 0;
        onConnect?.();
      };

      ws.onmessage = (event: MessageEvent): void => {
        try {
          const msg: WebSocketMessage = JSON.parse(event.data);
          setLastMessage(msg);
          onMessage?.(msg);
        } catch {
          console.warn('[WS] Failed to parse message:', event.data);
        }
      };

      ws.onclose = (): void => {
        setIsConnected(false);
        onDisconnect?.();
        // Auto-reconnect
        if (retriesRef.current < maxRetries) {
          retriesRef.current += 1;
          reconnectTimerRef.current = setTimeout(() => {
            connect();
          }, reconnectInterval);
        }
      };

      ws.onerror = (): void => {
        ws.close();
      };

      wsRef.current = ws;
    } catch (err) {
      console.error('[WS] Connection error:', err);
    }
  }, [symbol, onMessage, onConnect, onDisconnect, reconnectInterval, maxRetries]);

  const sendMessage = useCallback((msg: Partial<WebSocketMessage>): void => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  }, []);

  const reconnect = useCallback((): void => {
    if (wsRef.current) {
      wsRef.current.close();
    }
    retriesRef.current = 0;
    setTimeout(connect, 100);
  }, [connect]);

  useEffect(() => {
    connect();
    return (): void => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [symbol, connect]);

  return { isConnected, lastMessage, sendMessage, reconnect };
}
