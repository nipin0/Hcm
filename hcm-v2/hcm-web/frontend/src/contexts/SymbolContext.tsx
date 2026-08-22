import React, { createContext, useContext, useState, useCallback, ReactNode } from 'react';

export interface SymbolInfo {
  symbol: string;
  name: string;
  exchange: string;
  category: string;
  pipSize: number;
  contractSize: number;
}

interface SymbolContextType {
  selectedSymbol: SymbolInfo | null;
  setSelectedSymbol: (symbol: SymbolInfo) => void;
  symbolList: SymbolInfo[];
  setSymbolList: (symbols: SymbolInfo[]) => void;
}

const defaultSymbols: SymbolInfo[] = [
  { symbol: 'XAUUSD', name: '黄金/美元', exchange: 'FOREX', category: '贵金属', pipSize: 0.01, contractSize: 100 },
  { symbol: 'EURUSD', name: '欧元/美元', exchange: 'FOREX', category: '外汇', pipSize: 0.0001, contractSize: 100000 },
  { symbol: 'GBPUSD', name: '英镑/美元', exchange: 'FOREX', category: '外汇', pipSize: 0.0001, contractSize: 100000 },
  { symbol: 'USDJPY', name: '美元/日元', exchange: 'FOREX', category: '外汇', pipSize: 0.01, contractSize: 100000 },
  { symbol: 'US30', name: '道琼斯工业指数', exchange: 'INDEX', category: '指数', pipSize: 1.0, contractSize: 1 },
  { symbol: 'NAS100', name: '纳斯达克100', exchange: 'INDEX', category: '指数', pipSize: 1.0, contractSize: 1 },
];

const SymbolContext = createContext<SymbolContextType>({
  selectedSymbol: defaultSymbols[0],
  setSelectedSymbol: () => {},
  symbolList: defaultSymbols,
  setSymbolList: () => {},
});

export const useSymbol = (): SymbolContextType => useContext(SymbolContext);

interface SymbolProviderProps {
  children: ReactNode;
}

export const SymbolProvider: React.FC<SymbolProviderProps> = ({ children }) => {
  const [selectedSymbol, setSelectedSymbol] = useState<SymbolInfo>(defaultSymbols[0]);
  const [symbolList, setSymbolList] = useState<SymbolInfo[]>(defaultSymbols);

  const handleSetSymbol = useCallback((symbol: SymbolInfo): void => {
    setSelectedSymbol(symbol);
    // Dispatch custom event for cross-component communication
    window.dispatchEvent(new CustomEvent('symbolChanged', { detail: symbol }));
  }, []);

  const value: SymbolContextType = {
    selectedSymbol,
    setSelectedSymbol: handleSetSymbol,
    symbolList,
    setSymbolList,
  };

  return <SymbolContext.Provider value={value}>{children}</SymbolContext.Provider>;
};
