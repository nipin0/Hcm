import React from 'react';
import { Box, Select, MenuItem, Typography, SelectChangeEvent } from '@mui/material';
import { useSymbol, SymbolInfo } from '../contexts/SymbolContext';

const SymbolSelector: React.FC = () => {
  const { selectedSymbol, setSelectedSymbol, symbolList } = useSymbol();

  const handleChange = (event: SelectChangeEvent<string>): void => {
    const found: SymbolInfo | undefined = symbolList.find(
      (s) => s.symbol === event.target.value,
    );
    if (found) {
      setSelectedSymbol(found);
    }
  };

  return (
    <Box>
      <Typography variant="caption" className="text-gray-500 uppercase tracking-wider mb-1 block">
        当前品种
      </Typography>
      <Select
        fullWidth
        size="small"
        value={selectedSymbol?.symbol || ''}
        onChange={handleChange}
        sx={{
          '& .MuiSelect-select': {
            display: 'flex',
            alignItems: 'center',
            gap: 1,
            py: 1,
          },
          backgroundColor: '#1a1a24',
          borderRadius: 2,
          '& .MuiOutlinedInput-notchedOutline': {
            borderColor: '#2a2a3a',
          },
          '&:hover .MuiOutlinedInput-notchedOutline': {
            borderColor: '#3b82f6',
          },
          '&.Mui-focused .MuiOutlinedInput-notchedOutline': {
            borderColor: '#3b82f6',
          },
        }}
        MenuProps={{
          PaperProps: {
            sx: {
              backgroundColor: '#1a1a24',
              border: '1px solid #2a2a3a',
            },
          },
        }}
      >
        {symbolList.map((sym) => (
          <MenuItem key={sym.symbol} value={sym.symbol} dense>
            <Box className="flex items-center gap-2 w-full">
              <Typography sx={{ fontWeight: 600, fontSize: 13, color: '#f1f5f9' }}>
                {sym.symbol}
              </Typography>
              <Typography sx={{ fontSize: 11, color: '#64748b', ml: 'auto' }}>
                {sym.name}
              </Typography>
            </Box>
          </MenuItem>
        ))}
      </Select>
      {selectedSymbol && (
        <Box className="mt-2 flex gap-2 text-xs text-gray-500">
          <span>{selectedSymbol.exchange}</span>
          <span>·</span>
          <span>{selectedSymbol.category}</span>
        </Box>
      )}
    </Box>
  );
};

export default SymbolSelector;
