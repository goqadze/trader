// The dip buyer's saved setups: the shared picker (studies/SetupPicker.tsx) with the dip buyer's kit.

import type { ComponentProps } from "react";
import SetupPicker from "../studies/SetupPicker";
import { DIP_KIT } from "./kit";
import type { DipPresetConfig } from "./types";

export default function PresetPicker(props: Omit<ComponentProps<typeof SetupPicker<DipPresetConfig>>, "kit">) {
  return <SetupPicker kit={DIP_KIT} {...props} />;
}
