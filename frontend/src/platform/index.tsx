/**
 * 平台 Context + Provider
 * 顶层的 <PlatformProvider> 向整棵组件树注入平台信息，
 * 子组件通过 usePlatform() / useIsDesktop() 消费。
 */

import React, { createContext, useContext, useEffect, useState } from 'react';
import type { Platform } from './detect';
import { detectPlatform } from './detect';

interface PlatformContextValue {
  platform: Platform;
  isDesktop: boolean;
  isWeb: boolean;
}

const PlatformContext = createContext<PlatformContextValue>({
  platform: 'web',
  isDesktop: false,
  isWeb: true,
});

export const usePlatform = (): PlatformContextValue => useContext(PlatformContext);
export const useIsDesktop = (): boolean => useContext(PlatformContext).isDesktop;
export const useIsWeb = (): boolean => useContext(PlatformContext).isWeb;

export const PlatformProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [platform, setPlatform] = useState<Platform>('web');

  useEffect(() => {
    setPlatform(detectPlatform());
  }, []);

  const value: PlatformContextValue = {
    platform,
    isDesktop: platform === 'desktop',
    isWeb: platform === 'web',
  };

  return (
    <PlatformContext.Provider value={value}>
      {children}
    </PlatformContext.Provider>
  );
};
