export const COLORS = {
  bg: "#05070B", panel: "#000000", grid: "#1F2937", text: "#E5E7EB",
  muted: "#94A3B8", cyan: "#32E63A", yellow: "#FACC15", spot: "#CBD5E1",
  orange: "#6B2CFF", green: "#4ADE80", red: "#F87171", net: "#A3E635",
  bull: "#FFFFFF", bear: "#FF2600"
};
export const COLOR_STORAGE_KEYS = {
  cyan: "qqqDashboardCallColor",
  orange: "qqqDashboardPutColor",
  bull: "qqqDashboardBullColor",
  bear: "qqqDashboardBearColor"
};
export const LEGACY_DEFAULT_COLORS = {
  cyan: "#22D3EE",
  orange: "#F59E0B",
  bull: "#22D3EE",
  bear: "#F59E0B"
};

export const EXPOSURE_CONFIG = {
  net_gex: {label: "GEX Exposure", callKey: "call_gex", putKey: "put_gex", wallAbsKey: "gamma_wall_abs", flipKey: "gamma_flip", wallLabel: "GAMMA WALL", flipLabel: "GAMMA FLIP"},
  net_dex: {label: "DEX Exposure", callKey: "call_dex", putKey: "put_dex", wallAbsKey: "dex_wall_abs", flipKey: "delta_flip", wallLabel: "DELTA WALL", flipLabel: "DELTA FLIP"},
  net_vex: {label: "VEX Exposure", callKey: "call_vex", putKey: "put_vex", wallAbsKey: "vanna_wall_abs", flipKey: "vanna_flip", wallLabel: "VANNA WALL", flipLabel: "VANNA FLIP"},
  net_chex: {label: "CHEX Exposure", callKey: "call_chex", putKey: "put_chex", wallAbsKey: "charm_wall_abs", flipKey: "charm_flip", wallLabel: "CHARM WALL", flipLabel: "CHARM FLIP"},
};
