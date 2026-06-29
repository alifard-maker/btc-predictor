/**
 * Pure helpers for dashboard bot toggle sync (testable in Node).
 */
'use strict';

const BOT_SETTING_FIELDS = [
  'enabled',
  'mode',
  'allow_strong',
  'allow_actionable',
  'use_accumulated_profit',
  'paper_auto_refill',
];

const PATCH_CONFIRM_MS = 120000;

function botUiKey(kind, asset) {
  return `${kind}-${asset}`;
}

function normalizeBotSettings(raw, maxKey) {
  if (!raw) return null;
  return {
    enabled: !!raw.enabled,
    mode: raw.mode === 'live' ? 'live' : 'paper',
    [maxKey]: Number(raw[maxKey] ?? 25),
    allow_strong: !!raw.allow_strong,
    allow_actionable: !!raw.allow_actionable,
    use_accumulated_profit: !!raw.use_accumulated_profit,
    paper_auto_refill: raw.paper_auto_refill !== false,
  };
}

function botSettingsEqual(a, b, maxKey) {
  const na = normalizeBotSettings(a, maxKey);
  const nb = normalizeBotSettings(b, maxKey);
  if (!na || !nb) return na === nb;
  for (const field of BOT_SETTING_FIELDS) {
    if (na[field] !== nb[field]) return false;
  }
  return na[maxKey] === nb[maxKey];
}

function mergeBotSettings(server, prev) {
  if (!server && !prev) return null;
  if (!server) return prev ? { ...prev } : null;
  if (!prev) return { ...server };
  return { ...prev, ...server };
}

/** True when server settings should replace DOM (not during an in-flight PATCH). */
function shouldUpdateSettingsFromServer({
  server,
  dom,
  lastKnown,
  pending,
  patchConfirmed,
  maxKey,
}) {
  if (pending) return false;
  const srv = normalizeBotSettings(server, maxKey);
  if (!srv) return false;
  if (!dom) return true;

  const domNorm = normalizeBotSettings(dom, maxKey);
  const known = lastKnown ? normalizeBotSettings(lastKnown, maxKey) : domNorm;

  if (patchConfirmed && patchConfirmed.at && patchConfirmed.settings) {
    const age = Date.now() - patchConfirmed.at;
    if (age >= 0 && age < PATCH_CONFIRM_MS) {
      const conf = normalizeBotSettings(patchConfirmed.settings, maxKey);
      if (conf && srv.enabled !== conf.enabled) return false;
    }
  }

  // Never let a stale poll turn auto-bet OFF while UI and last-known both say ON.
  if (domNorm && known && domNorm.enabled && known.enabled && !srv.enabled) {
    return false;
  }

  if (!known || !botSettingsEqual(srv, known, maxKey)) return true;
  return !botSettingsEqual(srv, domNorm, maxKey);
}

function settingsForDisplay(server, dom, pending, maxKey) {
  if (pending) return pending;
  if (dom && !shouldUpdateSettingsFromServer({ server, dom, lastKnown: dom, pending: null, maxKey })) {
    return dom;
  }
  return normalizeBotSettings(server, maxKey) || dom || normalizeBotSettings(server, maxKey);
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    BOT_SETTING_FIELDS,
    PATCH_CONFIRM_MS,
    botUiKey,
    normalizeBotSettings,
    botSettingsEqual,
    mergeBotSettings,
    shouldUpdateSettingsFromServer,
    settingsForDisplay,
  };
}

if (typeof window !== 'undefined') {
  window.BotSettingsUi = {
    BOT_SETTING_FIELDS,
    PATCH_CONFIRM_MS,
    botUiKey,
    normalizeBotSettings,
    botSettingsEqual,
    mergeBotSettings,
    shouldUpdateSettingsFromServer,
    settingsForDisplay,
  };
}
