import React from 'react';

/** Inline SVG icon components replacing @mui/icons-material to avoid MUI v6 CJS/ESM resolution issues */

interface IconProps {
  sx?: React.CSSProperties;
  fontSize?: 'inherit' | 'small' | 'medium' | 'large';
  color?: string;
}

const sizeMap: Record<string, number> = { inherit: 24, small: 18, medium: 24, large: 32 };

const IconBase: React.FC<{ children: React.ReactNode; viewBox?: string } & IconProps> = ({
  children,
  viewBox = '0 0 24 24',
  fontSize = 'medium',
  sx,
}) => (
  <svg
    viewBox={viewBox}
    width={sizeMap[fontSize] || 24}
    height={sizeMap[fontSize] || 24}
    fill="currentColor"
    style={sx}
  >
    {children}
  </svg>
);

// ---- Navigation ----
export const Dashboard: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"/></IconBase>
);

export const Settings: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.488.488 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94L14.4 2.81a.484.484 0 0 0-.48-.41h-3.84c-.24 0-.43.17-.47.41L9.25 5.35c-.59.24-1.13.57-1.62.94l-2.39-.96a.476.476 0 0 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.07.62-.07.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></IconBase>
);

export const CellTower: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="m6.83 4.99-2.49 6.99H6l1.57 3.66L9 11h1.5l-.49-6.01H6.83zm7.52 6.98-1.58-3.64L11 11H9.93l1.43 6.03h1.07l1.59-3.77L15.5 11h-1.15zm6.93-6.98-2.12 6.98h.54l1.68-3.64L22 11h-1.41z"/></IconBase>
);

export const CloudDownload: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M19.35 10.04A7.49 7.49 0 0 0 12 4C9.11 4 6.6 5.64 5.35 8.04A5.994 5.994 0 0 0 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM19 18H6c-2.21 0-4-1.79-4-4s1.79-4 4-4h.71C7.37 7.69 9.48 6 12 6c3.04 0 5.5 2.46 5.5 5.5v.5H19c1.66 0 3 1.34 3 3s-1.34 3-3 3z"/></IconBase>
);

export const Psychology: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M13 3a9 9 0 0 0-9 9H1l3.89 3.89.07.14L9 12H6c0-3.87 3.13-7 7-7s7 3.13 7 7-3.13 7-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42A8.954 8.954 0 0 0 13 21a9 9 0 0 0 0-18zm-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8H12z"/></IconBase>
);

export const Send: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M2.01 21 23 12 2.01 3 2 10l15 2-15 2z"/></IconBase>
);

export const Shield: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M12 1 3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm0 10.99h7c-.53 4.12-3.28 7.79-7 8.94V12H5V6.3l7-3.11v8.8z"/></IconBase>
);

export const Cancel: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M12 2C6.47 2 2 6.47 2 12s4.47 10 10 10 10-4.47 10-10S17.53 2 12 2zm5 13.59L15.59 17 12 13.41 8.41 17 7 15.59 10.59 12 7 8.41 8.41 7 12 10.59 15.59 7 17 8.41 13.41 12 17 15.59z"/></IconBase>
);

export const ContentCopy: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></IconBase>
);

// ---- Actions ----
export const Add: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></IconBase>
);

export const Edit: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a.996.996 0 0 0 0-1.41l-2.34-2.34a.996.996 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></IconBase>
);

export const Delete: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></IconBase>
);

export const RefreshCw: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M17.65 6.35A7.958 7.958 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></IconBase>
);

export const DeleteSweep: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M15 16h4v2h-4v-2zm0-8h7v2h-7V8zm0 4h6v2h-6v-2zM3 18c0 1.1.9 2 2 2h6c1.1 0 2-.9 2-2V8H3v10zM14 5h-3l-1-1H6L5 5H2v2h12V5z"/></IconBase>
);

export const Save: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M17 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14c1.1 0 2-.9 2-2V7l-4-4zm-5 16c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3zm3-10H5V5h10v4z"/></IconBase>
);

export const Undo: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M12.5 8c-2.65 0-5.05.99-6.9 2.6L2 7v9h9l-3.62-3.62c1.39-1.16 3.16-1.88 5.12-1.88 3.54 0 6.55 2.31 7.6 5.5l2.37-.78C21.08 11.03 17.15 8 12.5 8z"/></IconBase>
);

// ---- Indicators ----
export const TrendingUp: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="m16 6 2.29 2.29-4.88 4.88-4-4L2 16.59 3.41 18l6-6 4 4 6.3-6.29L22 12V6z"/></IconBase>
);

export const TrendingDown: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="m16 18 2.29-2.29-4.88-4.88-4 4L2 7.41 3.41 6l6 6 4-4 6.3 6.29L22 12v6z"/></IconBase>
);

export const Login: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M11 7 9.6 8.4l2.6 2.6H2v2h10.2l-2.6 2.6L11 17l5-5-5-5zm9 12h-8v2h8c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2h-8v2h8v14z"/></IconBase>
);

export const Visibility: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z"/></IconBase>
);

export const VisibilityOff: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M12 7c2.76 0 5 2.24 5 5 0 .65-.13 1.26-.36 1.83l2.92 2.92c1.51-1.26 2.7-2.89 3.43-4.75-1.73-4.39-6-7.5-11-7.5-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7zM2 4.27l2.28 2.28.46.46A11.804 11.804 0 0 0 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84l.42.42L19.73 22 21 20.73 3.27 3 2 4.27zM7.53 9.8l1.55 1.55c-.05.21-.08.43-.08.65 0 1.66 1.34 3 3 3 .22 0 .44-.03.65-.08l1.55 1.55c-.67.33-1.41.53-2.2.53-2.76 0-5-2.24-5-5 0-.79.2-1.53.53-2.2zm4.31-.78 3.15 3.15.02-.16c0-1.66-1.34-3-3-3l-.17.01z"/></IconBase>
);

// ---- Status ----
export const Storage: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M2 20h20v-4H2v4zm2-3h2v2H4v-2zM2 4v4h20V4H2zm4 3H4V5h2v2zm-4 7h20v-4H2v4zm2-3h2v2H4v-2z"/></IconBase>
);

export const Memory: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M15 9H9v6h6V9zm-2 4h-2v-2h2v2zm8-2V9h-2V7c0-1.1-.9-2-2-2h-2V3h-2v2h-2V3H9v2H7c-1.1 0-2 .9-2 2v2H3v2h2v2H3v2h2v2c0 1.1.9 2 2 2h2v2h2v-2h2v2h2v-2h2c1.1 0 2-.9 2-2v-2h2v-2h-2v-2h2zm-4 6H7V7h10v10z"/></IconBase>
);

export const Hub: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M8.4 18.2c.38.5.6 1.12.6 1.8 0 1.66-1.34 3-3 3s-3-1.34-3-3 1.34-3 3-3c.44 0 .85.09 1.23.26l1.41-1.77a4.504 4.504 0 0 1-1.09-3.69l-2.03-.68c-.54.94-1.54 1.58-2.72 1.58-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3c0 .07 0 .14-.01.21l2.03.68c.64-1.21 1.82-2.09 3.22-2.32V4.42C9.96 3.6 9 2.4 9 1c0-1.66 1.34-3 3-3s3 1.34 3 3-1.34 3-3 3c-.3 0-.59-.05-.86-.13v2.83c1.41.22 2.6 1.1 3.24 2.32l2.03-.68c-.01-.07-.01-.14-.01-.21 0-1.66 1.34-3 3-3s3 1.34 3 3-1.34 3-3 3c-1.18 0-2.18-.64-2.72-1.58l-2.03.68a4.49 4.49 0 0 1-1.09 3.69l1.41 1.77c.38-.17.79-.26 1.23-.26 1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3c0-.68.22-1.3.6-1.8l-1.41-1.77c-.8.47-1.74.77-2.74.86v2.83c.27.08.56.13.86.13 1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3c0-1.4.96-2.6 2.27-2.92v-2.83c-1-.08-1.94-.38-2.73-.85L8.4 18.2z"/></IconBase>
);

export const FiberManualRecord: React.FC<IconProps> = (props) => (
  <IconBase {...props}><circle cx="12" cy="12" r="8"/></IconBase>
);

export const Wifi: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="m1 9 2 2c4.97-4.97 13.03-4.97 18 0l2-2C17.93 4.02 6.07 4.02 1 9zm8 8 3 3 3-3a4.237 4.237 0 0 0-6 0zm-4-4 2 2a7.074 7.074 0 0 1 10 0l2-2c-3.86-3.85-10.14-3.85-14 0z"/></IconBase>
);

// ---- Media Controls ----
export const Pause: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></IconBase>
);

export const PlayArrow: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M8 5v14l11-7z"/></IconBase>
);

export const RestartAlt: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M13 3a9 9 0 0 0-9 9H1l3.89 3.89.07.14L9 12H6c0-3.87 3.13-7 7-7s7 3.13 7 7-3.13 7-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42A8.954 8.954 0 0 0 13 21a9 9 0 0 0 0-18zm-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8H12z"/></IconBase>
);

export const DragIndicator: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M11 18c0 1.1-.9 2-2 2s-2-.9-2-2 .9-2 2-2 2 .9 2 2zm-2-8c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm0-6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm6 4c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0 2c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm0 6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z"/></IconBase>
);

export const Tune: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M3 17v2h6v-2H3zM3 5v2h10V5H3zm10 16v-2h8v-2h-8v-2h-2v6h2zM7 9v2H3v2h4v2h2V9H7zm14 4v-2H11v2h10zm-6-4h2V7h4V5h-4V3h-2v6z"/></IconBase>
);

export const CheckCircle: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></IconBase>
);

export const Error: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></IconBase>
);

export const Warning: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></IconBase>
);

export const HourglassEmpty: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M18 2H6v6l4 4-3.99 4.01L6 22h12l-.01-5.99L14 12l4-3.99V2zm0 14.5V20H6v-3.5l4-4-4-4V4h12v4.5l-4 4 4 4z"/></IconBase>
);

export const ExpandLess: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="m12 8-6 6 1.41 1.41L12 10.83l4.59 4.58L18 14z"/></IconBase>
);

export const ExpandMore: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M16.59 8.59 12 13.17 7.41 8.59 6 10l6 6 6-6z"/></IconBase>
);

export const ChevronRight: React.FC<IconProps> = (props) => (
  <IconBase {...props}><path d="M10 6 8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></IconBase>
);

