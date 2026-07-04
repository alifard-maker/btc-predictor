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
  'live_auto_refill_hour_budget',
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
    live_auto_refill_hour_budget: !!raw.live_auto_refill_hour_budget,
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
  // Live/paper mismatch is safety-critical — always trust server over stale DOM.
  if (domNorm && srv.mode && domNorm.mode !== srv.mode) return true;
  const known = lastKnown ? normalizeBotSettings(lastKnown, maxKey) : domNorm;

  if (patchConfirmed && patchConfirmed.at && patchConfirmed.settings) {
    const age = Date.now() - patchConfirmed.at;
    if (age >= 0 && age < PATCH_CONFIRM_MS) {
      const conf = normalizeBotSettings(patchConfirmed.settings, maxKey);
      if (conf) {
        if (srv.enabled !== conf.enabled) return false;
        if (srv[maxKey] !== conf[maxKey]) return false;
        if (srv.live_auto_refill_hour_budget !== conf.live_auto_refill_hour_budget) return false;
        if (srv.paper_auto_refill !== conf.paper_auto_refill) return false;
      }
    }
  }

  // Never let a stale poll flip auto-bet while DOM and last-known agree.
  if (domNorm && known && domNorm.enabled === known.enabled && domNorm.enabled !== srv.enabled) {
    return false;
  }

  // Never let a stale poll flip auto-refill while DOM and last-known agree.
  if (
    domNorm && known
    && domNorm.live_auto_refill_hour_budget === known.live_auto_refill_hour_budget
    && domNorm.live_auto_refill_hour_budget !== srv.live_auto_refill_hour_budget
  ) {
    return false;
  }
  if (
    domNorm && known
    && domNorm.paper_auto_refill === known.paper_auto_refill
    && domNorm.paper_auto_refill !== srv.paper_auto_refill
  ) {
    return false;
  }

  // Never clobber max cap input from a stale poll while DOM shows a different value.
  if (domNorm && srv && domNorm[maxKey] !== srv[maxKey]) {
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
