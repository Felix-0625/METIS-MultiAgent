/**
 * 跨平台通知服务
 * 自动选择 Tauri 原生通知或浏览器 Web Notifications API
 */

import { isTauri } from './platform';

interface NotificationOptions {
  title: string;
  body: string;
  icon?: string;
}

class NotificationService {
  private tauriNotificationModule: any = null;

  async init() {
    if (isTauri()) {
      try {
        // 动态导入 Tauri notification 模块
        // 使用 Function 构造器避免 TypeScript 编译时检查模块是否存在
        const importTauri = new Function('moduleName', 'return import(moduleName)');
        const tauriNotification = await importTauri('@tauri-apps/api/notification');
        
        this.tauriNotificationModule = {
          sendNotification: tauriNotification.sendNotification,
          isPermissionGranted: tauriNotification.isPermissionGranted,
          requestPermission: tauriNotification.requestPermission,
        };
        
        // 请求权限
        const permitted = await tauriNotification.isPermissionGranted();
        if (!permitted) {
          await tauriNotification.requestPermission();
        }
      } catch (error) {
        console.warn('[Notifications] Tauri 通知模块加载失败（仅桌面端可用）:', error);
      }
    } else {
      // 浏览器环境，请求权限
      if ('Notification' in window && Notification.permission === 'default') {
        await Notification.requestPermission();
      }
    }
  }

  async send(options: NotificationOptions) {
    try {
      if (isTauri() && this.tauriNotificationModule) {
        // Tauri 原生通知
        await this.tauriNotificationModule.sendNotification({
          title: options.title,
          body: options.body,
          icon: options.icon,
        });
      } else if ('Notification' in window && Notification.permission === 'granted') {
        // 浏览器 Web Notifications
        new Notification(options.title, {
          body: options.body,
          icon: options.icon || '/favicon.ico',
        });
      } else {
        console.warn('[Notifications] 通知权限未授予');
      }
    } catch (error) {
      console.error('[Notifications] 发送通知失败:', error);
    }
  }

  // 快捷方法
  async success(title: string, body: string) {
    await this.send({ title, body, icon: '/icons/success.png' });
  }

  async error(title: string, body: string) {
    await this.send({ title, body, icon: '/icons/error.png' });
  }

  async info(title: string, body: string) {
    await this.send({ title, body, icon: '/icons/info.png' });
  }

  async warning(title: string, body: string) {
    await this.send({ title, body, icon: '/icons/warning.png' });
  }
}

export const notificationService = new NotificationService();
