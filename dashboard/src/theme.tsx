import { useLayoutEffect, type ReactNode } from 'react';
import { App, ConfigProvider, theme, type ThemeConfig } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { useLocation } from 'react-router-dom';

// Ant Design and custom CSS share the same semantic palette, including body portals.
const palettes = {
  light: {
    canvas: '#f4f6f9', surface: '#ffffff', elevated: '#ffffff', subtle: '#f0f3f7',
    hover: '#eaf0f8', border: '#d6dde7', divider: '#e5e9f0',
    text: '#202b3d', secondary: '#536278', muted: '#64748b', disabled: '#8b97a8',
    primary: '#365fc7', primaryHover: '#254ba9', accentBg: '#eaf0ff', accentBorder: '#b8c9ef', onPrimary: '#ffffff',
    success: '#26785e', successBg: '#eaf5ef', successBorder: '#afd6c1',
    warning: '#936019', warningBg: '#fff5e5', warningBorder: '#e4c48d',
    error: '#c33e48', purple: '#7255ad', purpleBg: '#f1ecfa',
    flow: '#367f9d', feedback: '#8268b0', grid: '#d7dee9',
    milestoneBg: '#ffffff', milestoneText: '#202b3d', milestoneMuted: '#536278', milestoneBorder: '#b8c9df', milestoneBadge: '#e7edf5',
    shadow: '#202b3d0d', mask: '#10172266',
  },
  dark: {
    canvas: '#11151c', surface: '#181e27', elevated: '#202835', subtle: '#1d2531',
    hover: '#283345', border: '#3b485b', divider: '#2c3747',
    text: '#e6edf6', secondary: '#b7c3d4', muted: '#95a5bb', disabled: '#687991',
    primary: '#8aafff', primaryHover: '#b3cbff', accentBg: '#213452', accentBorder: '#4b6f9f', onPrimary: '#111d34',
    success: '#7dceb0', successBg: '#1b352f', successBorder: '#396556',
    warning: '#e8bc78', warningBg: '#352d21', warningBorder: '#7c633c',
    error: '#f28c94', purple: '#b5a1e1', purpleBg: '#2b2640',
    flow: '#78b8ce', feedback: '#aa96d1', grid: '#2b3647',
    milestoneBg: '#f3f6fa', milestoneText: '#202b3d', milestoneMuted: '#536278', milestoneBorder: '#d6dde7', milestoneBadge: '#e1e8f1',
    shadow: '#00000026', mask: '#080d1666',
  },
};

function createTheme(mode: keyof typeof palettes): ThemeConfig {
  const p = palettes[mode];
  return {
    algorithm: mode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: {
      colorPrimary: p.primary, colorInfo: p.primary, colorLink: p.primary,
      colorLinkHover: p.primaryHover, colorLinkActive: p.primary,
      colorSuccess: p.success, colorWarning: p.warning, colorError: p.error,
      colorBgLayout: p.canvas, colorBgContainer: p.surface, colorBgElevated: p.elevated,
      colorText: p.text, colorTextSecondary: p.secondary, colorTextTertiary: p.muted,
      colorTextQuaternary: p.disabled, colorTextPlaceholder: p.muted, colorTextDisabled: p.disabled,
      colorBorder: p.border, colorBorderSecondary: p.divider, colorSplit: p.divider,
      colorFillAlter: p.subtle, colorBgContainerDisabled: p.subtle, colorBgTextHover: p.hover,
      colorTextLightSolid: p.onPrimary, colorBgMask: p.mask,
      borderRadius: 10,
      fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif',
    },
    components: {
      Table: { headerBg: p.subtle, headerColor: p.secondary, headerSplitColor: p.divider,
        borderColor: p.divider, rowHoverBg: p.hover, rowSelectedBg: p.accentBg,
        rowSelectedHoverBg: p.hover, headerSortActiveBg: p.hover, headerSortHoverBg: p.hover,
        bodySortBg: p.subtle, footerBg: p.subtle, cellPaddingBlock: 18 },
      Button: { controlHeight: 38, primaryColor: p.onPrimary, primaryShadow: 'none' },
      Card: { headerFontSize: 15 },
      Tabs: { itemColor: p.muted, itemSelectedColor: p.primary, inkBarColor: p.primary },
      Select: { optionSelectedBg: p.accentBg, optionActiveBg: p.hover },
      Tooltip: { colorBgSpotlight: mode === 'dark' ? '#344259' : '#202b3d', colorTextLightSolid: '#f5f8ff' },
    },
  };
}
const themes = { light: createTheme('light'), dark: createTheme('dark') };

export default function DashboardTheme({ children }: { children: ReactNode }) {
  const { pathname, search } = useLocation();
  const dialogue = pathname === '/autopilot' && new URLSearchParams(search).get('view') === 'intelligence';
  const mode = !dialogue && (pathname === '/' || pathname === '/autopilot') ? 'dark' : 'light';
  useLayoutEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = mode;
    for (const [key, value] of Object.entries(palettes[mode])) root.style.setProperty(`--${key}`, value);
    return () => {
      delete root.dataset.theme;
      for (const key of Object.keys(palettes[mode])) root.style.removeProperty(`--${key}`);
    };
  }, [mode]);
  return <ConfigProvider button={{ autoInsertSpace: false }} locale={zhCN} theme={themes[mode]}>
    <App>{children}</App>
  </ConfigProvider>;
}
