import { invoke, isTauri } from '@tauri-apps/api/core';

export type Environment = 'development' | 'production';
export type SystemInfo = {
  environment: Environment;
  instance_id: string;
  app_version: string;
  api_version: string;
  ready: boolean;
  reason: string | null;
};
export type SessionInfo = { username: string; expires_at: string };
export type WorkspaceInfo = {
  message: string;
  environment: Environment;
  instance_id: string;
  username: string;
  database: string;
};
export type ClientSnapshot = {
  environment: Environment;
  baseUrl: string | null;
  system: SystemInfo | null;
  session: SessionInfo | null;
  workspace: WorkspaceInfo | null;
  notice: string | null;
};
export type ClientError = { code: string; message: string };
export type NativeCommand = 'bootstrap' | 'connect_service' | 'save_connection' | 'login' | 'refresh_workspace' | 'logout';

export function hasNativeBridge(): boolean { return isTauri(); }

export async function nativeCommand(command: NativeCommand, args?: Record<string, unknown>): Promise<ClientSnapshot> {
  if (!hasNativeBridge()) {
    throw { code: 'NATIVE_BRIDGE_UNAVAILABLE', message: '请在桌面应用中连接服务。浏览器预览无法调用原生连接与凭据功能。' } satisfies ClientError;
  }
  return invoke<ClientSnapshot>(command, args);
}

export function describeError(error: unknown): ClientError {
  if (typeof error === 'object' && error !== null && 'message' in error && typeof error.message === 'string') {
    return { code: 'code' in error && typeof error.code === 'string' ? error.code : 'CLIENT_ERROR', message: error.message };
  }
  return { code: 'CLIENT_ERROR', message: '操作未完成，请重试；若问题持续，请检查服务连接。' };
}

export function formatEnvironment(value: Environment): string {
  return value === 'development' ? '开发环境' : '正式环境';
}

export function formatExpiry(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '由服务端管理' : new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}
