import { describe, expect, it } from 'vitest';
import { describeError, formatExpiry } from './native';

describe('native error boundary', () => {
  it('preserves actionable server error codes without exposing arbitrary thrown data', () => {
    expect(describeError({ code: 'ENVIRONMENT_MISMATCH', message: '连接的服务环境不匹配。', token: 'must-not-surface' }))
      .toEqual({ code: 'ENVIRONMENT_MISMATCH', message: '连接的服务环境不匹配。' });
    expect(describeError('raw internal failure')).toEqual({ code: 'CLIENT_ERROR', message: '操作未完成，请重试；若问题持续，请检查服务连接。' });
  });
  it('does not display an invalid server timestamp as a local date', () => {
    expect(formatExpiry('not-a-date')).toBe('由服务端管理');
  });
});
