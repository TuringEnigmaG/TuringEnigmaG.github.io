// 12 人预女猎守（4 狼 4 民 + 预言家/女巫/猎人/守卫，屠边）的简化模拟器。浏览器与 node 共用。
// 这是一个玩具模型：每位好人对每个玩家有一个带噪声的"狼味"读数，信息（预言家查验）在其上叠加。
(function (root) {
  function mulberry32(a) { return function () { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
  function gauss(r) { let u = 0, v = 0; while (u === 0) u = r(); v = r(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); }
  const ROLES = ['wolf', 'wolf', 'wolf', 'wolf', 'villager', 'villager', 'villager', 'villager', 'seer', 'witch', 'hunter', 'guard'];
  const DEFAULTS = {
    skill: 1.9,          // 好人读人能力：狼在噪声读数里多出的信号强度
    seerReveal: 'day1',  // day1 | onWolf | never
    witchNight1: 'save', // save | keep
    witchPoison: true,   // 是否在确信时毒人
    wolfFake: 'none',    // none | fake  （悍跳预言家）
    guardStyle: 'empty', // empty（首夜空守）| self | seer（守跳出来的预言家）
    trustReal: 0.6,      // 两个预言家对跳时，好人相信真预言家的概率
  };

  function play(cfg, r, log) {
    cfg = Object.assign({}, DEFAULTS, cfg);
    const n = 12, role = ROLES.slice();
    for (let i = n - 1; i > 0; i--) { const j = Math.floor(r() * (i + 1)); [role[i], role[j]] = [role[j], role[i]]; }
    const alive = Array(n).fill(true), isWolf = i => role[i] === 'wolf', say = s => log && log.push(s);
    const idx = name => role.indexOf(name);
    // 好人的私有读数（持久）
    const read = Array.from({ length: n }, () => Array.from({ length: n }, (_, j) => (isWolf(j) ? cfg.skill : 0) + gauss(r)));
    let antidote = true, poison = true, guardLast = -1, seerClaimed = false, fakeSeer = -1;
    const checks = []; // 真预言家查验结果 {t, wolf}
    const fakeChecks = []; // 假预言家报的"查杀"
    const trust = Array(n).fill(null); // 对跳时每个好人相信谁
    const gods = ['seer', 'witch', 'hunter', 'guard'];
    const P = i => `P${i + 1}`;
    const nameOf = i => `${P(i)} (${role[i]})`;

    function winner() {
      const wolves = role.filter((x, i) => alive[i] && x === 'wolf').length;
      const vill = role.filter((x, i) => alive[i] && x === 'villager').length;
      const god = role.filter((x, i) => alive[i] && gods.includes(x)).length;
      if (wolves === 0) return 'good';
      if (vill === 0 || god === 0) return 'wolves';
      if (wolves >= vill + god) return 'wolves';
      return null;
    }
    // 好人 i 眼中 j 的狼味
    function suspicion(i, j) {
      let s = read[i][j];
      const seer = idx('seer');
      if (role[i] === 'seer') { const c = checks.find(c => c.t === j); if (c) return c.wolf ? 99 : -99; return s; }
      if (seerClaimed || fakeSeer >= 0) {
        const believe = fakeSeer >= 0 && seerClaimed ? trust[i] : (seerClaimed ? seer : fakeSeer);
        if (believe != null && believe >= 0) {
          if (j === believe) s -= 3;
          const list = believe === seer ? checks : fakeChecks;
          const c = list.find(c => c.t === j);
          if (c) s += c.wolf ? 4 : -3;
          const other = believe === seer ? fakeSeer : (seerClaimed ? seer : -1);
          if (other >= 0 && j === other) s += 3;
        }
      }
      return s;
    }
    function hunterShot(h) {
      const cands = [...Array(n).keys()].filter(j => alive[j] && j !== h);
      const t = cands.sort((a, b) => suspicion(h, b) - suspicion(h, a))[0];
      if (t != null) { alive[t] = false; say(`Hunter ${P(h)} shoots ${nameOf(t)}.`); }
    }
    function kill(i, how) { alive[i] = false; say(`${nameOf(i)} ${how}.`); if (role[i] === 'hunter' && how !== 'is poisoned') hunterShot(i); }

    for (let day = 1; day <= 12; day++) {
      // ---------- 夜晚 ----------
      say(`Night ${day}`);
      const seer = idx('seer'), witch = idx('witch'), guard = idx('guard');
      let guarded = -1;
      if (alive[guard]) {
        let g = -1;
        if (cfg.guardStyle === 'self') g = guard;
        else if (cfg.guardStyle === 'seer' && seerClaimed && alive[seer]) g = seer;
        else if (day > 1) { const c = [...Array(n).keys()].filter(j => alive[j]); g = c[Math.floor(r() * c.length)]; }
        if (g === guardLast) g = -1;
        guarded = g; guardLast = g;
      }
      // 狼刀：优先跳出来的真预言家，其次随机好人
      const goodAlive = [...Array(n).keys()].filter(j => alive[j] && !isWolf(j));
      let victim = seerClaimed && alive[seer] ? seer : goodAlive[Math.floor(r() * goodAlive.length)];
      let saved = false, poisoned = -1;
      if (alive[witch]) {
        if (antidote && (day > 1 ? victim === seer && seerClaimed : cfg.witchNight1 === 'save')) { saved = true; antidote = false; say(`Witch uses the antidote on ${P(victim)}.`); }
        if (cfg.witchPoison && poison) {
          const cands = [...Array(n).keys()].filter(j => alive[j] && j !== witch);
          const top = cands.sort((a, b) => suspicion(witch, b) - suspicion(witch, a))[0];
          if (top != null && suspicion(witch, top) > 3.2) { poisoned = top; poison = false; }
        }
      }
      // 预言家查验
      if (alive[seer]) {
        const unk = [...Array(n).keys()].filter(j => alive[j] && j !== seer && !checks.some(c => c.t === j));
        if (unk.length) { const t = unk[Math.floor(r() * unk.length)]; checks.push({ t, wolf: isWolf(t), day }); }
      }
      const protectedTwice = saved && guarded === victim;
      if ((!saved && guarded !== victim) || protectedTwice) kill(victim, protectedTwice ? 'dies anyway: guarded and saved on the same night' : 'is killed by the wolves');
      else say('Peaceful night.');
      if (poisoned >= 0 && alive[poisoned]) kill(poisoned, 'is poisoned');
      let w = winner(); if (w) return { winner: w, days: day };

      // ---------- 白天 ----------
      say(`Day ${day}`);
      if (alive[seer] && !seerClaimed) {
        if (cfg.seerReveal === 'day1' || (cfg.seerReveal === 'onWolf' && checks.some(c => c.wolf))) { seerClaimed = true; say(`${P(seer)} claims seer: ${checks.map(c => `${P(c.t)} ${c.wolf ? 'wolf' : 'good'}`).join(', ')}.`); }
      }
      if (cfg.wolfFake === 'fake' && fakeSeer < 0 && (seerClaimed || day === 1)) {
        const ws = [...Array(n).keys()].filter(j => alive[j] && isWolf(j));
        if (ws.length) {
          fakeSeer = ws[0];
          const target = seerClaimed && alive[seer] ? seer : goodAlive[Math.floor(r() * goodAlive.length)];
          fakeChecks.push({ t: target, wolf: true });
          say(`${P(fakeSeer)} counter-claims seer and calls ${P(target)} a wolf.`);
        }
      }
      if (fakeSeer >= 0 && alive[fakeSeer] && fakeChecks.length) {
        const g2 = [...Array(n).keys()].filter(j => alive[j] && !isWolf(j) && !fakeChecks.some(c => c.t === j));
        if (day > 1 && g2.length) fakeChecks.push({ t: g2[Math.floor(r() * g2.length)], wolf: r() < .5 });
      }
      if (seerClaimed && fakeSeer >= 0) for (let i = 0; i < n; i++) if (trust[i] == null && !isWolf(i)) trust[i] = r() < cfg.trustReal ? seer : fakeSeer;
      // 投票
      const votes = Array(n).fill(0), goodVotes = Array(n).fill(0);
      for (let i = 0; i < n; i++) {
        if (!alive[i] || isWolf(i)) continue;
        const c = [...Array(n).keys()].filter(j => alive[j] && j !== i);
        const t = c.sort((a, b) => (suspicion(i, b) + gauss(r) * .3) - (suspicion(i, a) + gauss(r) * .3))[0];
        votes[t]++; goodVotes[t]++;
      }
      const wolfTarget = (seerClaimed && alive[seer]) ? seer : [...Array(n).keys()].filter(j => alive[j] && !isWolf(j)).sort((a, b) => goodVotes[b] - goodVotes[a])[0];
      for (let i = 0; i < n; i++) if (alive[i] && isWolf(i)) votes[wolfTarget]++;
      const max = Math.max(...votes), top = votes.map((v, j) => v === max ? j : -1).filter(j => j >= 0);
      if (top.length === 1 && max > 0) kill(top[0], `is voted out (${max} votes)`); else say('Tie vote: nobody is exiled.');
      w = winner(); if (w) return { winner: w, days: day };
    }
    return { winner: 'wolves', days: 12 };
  }

  function run(cfg, games, seed) {
    const r = mulberry32(seed); let good = 0, days = 0;
    for (let g = 0; g < games; g++) { const o = play(cfg, r); if (o.winner === 'good') good++; days += o.days; }
    return { games, good, rate: good / games, avgDays: days / games };
  }
  const api = { play, run, mulberry32, DEFAULTS, ROLES };
  if (typeof module !== 'undefined') module.exports = api; else root.WW = api;
})(this);
