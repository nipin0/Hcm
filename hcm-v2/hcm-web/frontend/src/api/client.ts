import axios, { AxiosInstance, InternalAxiosRequestConfig, AxiosResponse, AxiosError } from 'axios';

// Use relative URL — works for localhost and LAN access
const BASE_URL: string = '';

const client: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
});

/** Request interceptor: inject JWT token from localStorage */
client.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token: string | null = localStorage.getItem('hcm_token');
    if (token && config.headers) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error: AxiosError) => Promise.reject(error),
);

/**
 * Single-flight refresh lock + waiting queue.
 *
 * 根因修复：后端 /api/auth/refresh 是「旋转式」refresh token（每次返回新的
 * refresh_token，旧 token 立即作废）。当 access token 过期瞬间，多个常驻轮询
 * （StatusBar 15s、Statistics 3s×N、Health 15s 等）会同时收到 401 并各自发起
 * refresh —— 除第一个外都带着已被作废的旧 refresh token，导致 refresh 失败 →
 * 误清除 token 并整页跳 /login（表现为「闪一下即退出」，且任意页面都会发生）。
 *
 * 用单飞锁保证同一时刻只发一个 refresh 请求，其余 401 进入等待队列，刷新成功
 * 后统一用新 token 重试，从而消除并发 refresh 竞争导致的误登出。
 */
let isRefreshing = false;
let pendingQueue: Array<(token: string | null) => void> = [];

function processQueue(error: unknown, token: string | null = null): void {
  pendingQueue.forEach((cb) => cb(token));
  pendingQueue = [];
}

client.interceptors.response.use(
  (response: AxiosResponse) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & { _retry?: boolean };

    // 仅对未重试过的 401 触发刷新逻辑
    if (error.response?.status !== 401 || originalRequest._retry) {
      return Promise.reject(error);
    }
    originalRequest._retry = true;

    // 已有 refresh 在进行中：排队等待，复用其结果，避免并发竞争旋转 token
    if (isRefreshing) {
      return new Promise((resolve, reject) => {
        pendingQueue.push((token: string | null) => {
          if (token) {
            if (originalRequest.headers) {
              originalRequest.headers.Authorization = `Bearer ${token}`;
            }
            resolve(client(originalRequest));
          } else {
            reject(error);
          }
        });
      });
    }

    const refreshToken: string | null = localStorage.getItem('hcm_refresh_token');
    if (!refreshToken) {
      window.location.href = '/login';
      return Promise.reject(error);
    }

    isRefreshing = true;
    try {
      const { data } = await axios.post(`${BASE_URL}/api/auth/refresh`, {
        refresh_token: refreshToken,
      });
      localStorage.setItem('hcm_token', data.access_token);
      localStorage.setItem('hcm_refresh_token', data.refresh_token);
      processQueue(null, data.access_token);
      if (originalRequest.headers) {
        originalRequest.headers.Authorization = `Bearer ${data.access_token}`;
      }
      return client(originalRequest);
    } catch (refreshError) {
      // refresh 真正失败（refresh token 也过期）→ 仅此处才退出登录
      processQueue(refreshError, null);
      localStorage.removeItem('hcm_token');
      localStorage.removeItem('hcm_refresh_token');
      window.location.href = '/login';
      return Promise.reject(refreshError);
    } finally {
      isRefreshing = false;
    }
  },
);

export default client;
