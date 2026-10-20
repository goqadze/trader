// A bot's stars, 0 to 5 (trading-service PUT /bots/{id}/rating): the owner's favourites. Set in the bot table's
// Rating column, which sorts by them, and on the bot's page. Only a label: nothing about how the bot trades.

import { App as AntApp, Rate } from "antd";
import { useEffect, useState } from "react";
import { tradingApi } from "./api";
import type { Bot } from "./types";

/** Click a star to rate, the same star again to clear it. The new stars show at once, the old ones come back if the
 * save fails; onChanged reloads the list so a table sorted by rating moves the bot. */
export function BotRating({ bot, onChanged, size = 14 }: { bot: Bot; onChanged: () => void; size?: number }) {
  const { message } = AntApp.useApp();
  const [value, setValue] = useState(bot.rating);
  useEffect(() => setValue(bot.rating), [bot.rating]);

  const save = async (rating: number) => {
    const before = value;
    setValue(rating);
    try {
      await tradingApi.setRating(bot.id, rating);
      onChanged();
    } catch (e) {
      setValue(before);
      message.error(`Couldn't save the stars: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  return (
    // In the bot table a click on a star rates: it mustn't also reach the row, which opens the bot's page
    <span onClick={(e) => e.stopPropagation()} style={{ whiteSpace: "nowrap" }}>
      <Rate value={value} onChange={save} style={{ fontSize: size }} aria-label={`Rating of ${bot.name}`} />
    </span>
  );
}
