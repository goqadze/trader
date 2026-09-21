import { Card } from "antd";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend } from "recharts";
import type { ChartPoint } from "../types";

// Strategy equity vs. a passive buy & hold benchmark, updated live as steps arrive.
export default function EquityChart({ data }: { data: ChartPoint[] }) {
  return (
    <Card title="Equity vs. Buy & Hold" size="small" style={{ marginTop: 16 }}>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={data}>
          <CartesianGrid stroke="#2a3550" />
          <XAxis dataKey="date" tick={{ fill: "#8b98b5", fontSize: 11 }} minTickGap={40} />
          <YAxis tick={{ fill: "#8b98b5", fontSize: 11 }} width={70} domain={["auto", "auto"]} />
          <Tooltip contentStyle={{ background: "#182031", border: "1px solid #2a3550" }} labelStyle={{ color: "#e6ebf5" }} />
          <Legend />
          <Line type="monotone" dataKey="strategy" name="Strategy" stroke="#4f8cff" dot={false} strokeWidth={2} isAnimationActive={false} />
          <Line type="monotone" dataKey="buyhold" name="Buy & Hold" stroke="#8b98b5" dot={false} strokeDasharray="5 4" isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </Card>
  );
}
