/**
 * ECharts 模块注册 —— **必须集中注册，否则 tree-shaking 入口会静默空白**
 * （`echarts-for-react/lib/core` 不注册不报错，只画出一片空图）
 *
 * 本项目原先全仓无 candlestick 用例（grep `candlestick` = 0 命中），
 * 故本文件把所有需要的模块一次性注册好，供 K 线图与概率图共用。
 */
import * as echarts from 'echarts/core';
import { LineChart, CandlestickChart } from 'echarts/charts';
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkLineComponent,   // 箱体三线 / 状态切换竖线
  MarkAreaComponent,   // 行情状态背景色块
  MarkPointComponent,  // 开平仓标记点
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  LineChart,
  CandlestickChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkLineComponent,
  MarkAreaComponent,
  MarkPointComponent,
  CanvasRenderer,
]);

export default echarts;
