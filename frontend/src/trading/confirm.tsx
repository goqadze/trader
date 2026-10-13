// Asking before an action that changes a bot or trades: a modal says what will happen and to which bot (a live one in
// red); OK does it, its button spinning until it's done, Cancel leaves everything as it was.

import { App as AntApp, Space, Tag } from "antd";
import type { ReactNode } from "react";
import { accountLabel } from "./format";
import type { Bot } from "./types";

const MUTED = "#8b98b5";

/** The bot's account as a tag: red "LIVE — real money", or the paper account it trades. */
export const AccountTag = ({ bot }: { bot: Pick<Bot, "live" | "broker"> }) => (
  <Tag color={bot.live ? "red" : "blue"} style={{ marginInlineEnd: 0 }}>{accountLabel(bot)}</Tag>
);

export interface Ask {
  title: string;
  content: ReactNode;
  okText: string;
  danger?: boolean;
  bot?: Bot; // named under the text, with its account
  onOk: () => Promise<unknown>;
}

export function useConfirm() {
  const { modal } = AntApp.useApp();
  return ({ title, content, okText, danger, bot, onOk }: Ask) => {
    modal.confirm({
      title,
      content: (
        <Space direction="vertical" size={10} style={{ marginTop: 4 }}>
          <span>{content}</span>
          {bot && <Space size={6} wrap><span style={{ color: MUTED }}>{bot.name}</span><AccountTag bot={bot} /></Space>}
        </Space>
      ),
      okText,
      okButtonProps: danger ? { danger: true } : undefined,
      onOk,
    });
  };
}
