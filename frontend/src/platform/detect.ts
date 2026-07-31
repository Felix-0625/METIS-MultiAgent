/**
 * 平台环境检测
 * 在模块加载时立即执行，无副作用
 *
 * detectPlatform() 在首次调用后缓存结果（惰性求值）
 */

export type Platform = 'desktop' | 'web';

let _cached: Platform | null = null;

function detectRaw(): Platform {
  if (typeof window !== 'undefined') {
    if ('__TAURI__' in window) return 'desktop';
    if ('__TAURI_INTERNALS__' in window) return 'desktop';
  }
  return 'web';
}

/** 检测当前运行环境，结果惰性缓存 */
export function detectPlatform(): Platform {
  if (_cached === null) {
    _cached = detectRaw();
  }
  return _cached;
}

export function isDesktop(): boolean {
  return detectPlatform() === 'desktop';
}

export function isWeb(): boolean {
  return detectPlatform() === 'web';
}
