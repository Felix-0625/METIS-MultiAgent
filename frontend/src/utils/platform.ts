/**
 * 平台检测工具
 * 检测当前运行环境（浏览器 vs Tauri 桌面端）
 */

// 检测是否在 Tauri 环境中运行
export const isTauri = (): boolean => {
  return typeof window !== 'undefined' && '__TAURI__' in window;
};

// 检测是否在浏览器环境中运行
export const isBrowser = (): boolean => {
  return !isTauri();
};

// 获取平台类型
export const getPlatform = (): 'tauri' | 'browser' => {
  return isTauri() ? 'tauri' : 'browser';
};

// 平台名称映射
export const getPlatformName = (): string => {
  if (isTauri()) {
    return 'Desktop';
  }
  return 'Web';
};

// 检测操作系统
export const getOS = (): 'windows' | 'macos' | 'linux' | 'unknown' => {
  if (typeof window === 'undefined') return 'unknown';
  
  const userAgent = window.navigator.userAgent.toLowerCase();
  
  if (userAgent.includes('win')) return 'windows';
  if (userAgent.includes('mac')) return 'macos';
  if (userAgent.includes('linux')) return 'linux';
  
  return 'unknown';
};

// 平台特性标志
export const platformFeatures = {
  // Tauri 专属功能
  get nativeNotifications(): boolean {
    return isTauri();
  },
  
  get nativeFileSystem(): boolean {
    return isTauri();
  },
  
  get systemTray(): boolean {
    return isTauri();
  },
  
  get globalShortcuts(): boolean {
    return isTauri();
  },
  
  // 浏览器功能
  get webNotifications(): boolean {
    return isBrowser() && 'Notification' in window;
  },
  
  get serviceWorker(): boolean {
    return isBrowser() && 'serviceWorker' in navigator;
  },
};

// 控制台输出平台信息
export const logPlatformInfo = () => {
  console.log('=== Platform Info ===');
  console.log('Platform:', getPlatform());
  console.log('Platform Name:', getPlatformName());
  console.log('OS:', getOS());
  console.log('Features:', platformFeatures);
  console.log('====================');
};
