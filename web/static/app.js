const API = '';

// ── Navigation ──
document.querySelectorAll('nav a').forEach(a => {
    a.addEventListener('click', e => {
        e.preventDefault();
        document.querySelectorAll('nav a').forEach(x => x.classList.remove('active'));
        a.classList.add('active');
        document.querySelectorAll('main > section').forEach(s => s.classList.add('hidden'));
        document.getElementById('page-' + a.dataset.page).classList.remove('hidden');
    });
});

// ── Data Fetching ──
async function fetchJSON(url) {
    const resp = await fetch(API + url);
    return resp.json();
}

async function refreshStatus() {
    const s = await fetchJSON('/api/status');
    document.getElementById('market-count').textContent = '市场: ' + s.market_count;
    document.getElementById('uptime').textContent = '运行: ' + Math.floor(s.uptime / 60) + 'm';
    document.getElementById('ws-status').textContent = '🟢 运行中';
}

async function refreshStats() {
    const s = await fetchJSON('/api/stats');
    document.getElementById('stat-violations').textContent = s.today_violations;
    document.getElementById('stat-trades').textContent = s.today_trades;
    document.getElementById('stat-profit').textContent = '$' + s.total_profit.toFixed(2);

    const status = await fetchJSON('/api/status');
    document.getElementById('stat-markets').textContent = status.market_count;
    document.getElementById('stat-relations').textContent = status.relation_count;
}

async function refreshSignals() {
    const data = await fetchJSON('/api/violations?limit=20');
    const tbody = document.querySelector('#recent-signals tbody');
    tbody.innerHTML = data.map(v =>
        `<tr><td>${v.detected_at || '-'}</td><td>${v.violation_amount.toFixed(4)}</td><td>${v.signal}</td></tr>`
    ).join('');
}

async function refreshMarkets() {
    const data = await fetchJSON('/api/markets');
    const tbody = document.querySelector('#market-table tbody');
    tbody.innerHTML = data.map(m =>
        `<tr><td>${m.slug}</td><td>${m.question}</td><td>${m.last_price ?? '-'}</td><td>${m.last_updated || '-'}</td></tr>`
    ).join('');
}

async function refreshRelations(status) {
    const url = status ? `/api/relations?status=${status}` : '/api/relations';
    const data = await fetchJSON(url);
    const tbody = document.querySelector('#relation-table tbody');
    tbody.innerHTML = data.map(r =>
        `<tr>
            <td>${r.id}</td><td>${r.market_a}</td><td>${r.market_b}</td>
            <td>${r.relation_type}</td><td>${r.description}</td>
            <td>${r.status === 'pending' ?
                `<button class="approve" onclick="approveRelation(${r.id})">Approve</button>` +
                `<button class="reject" onclick="rejectRelation(${r.id})">Reject</button>` : r.status
            }</td>
        </tr>`
    ).join('');
}

async function refreshTrades() {
    const data = await fetchJSON('/api/trades');
    const tbody = document.querySelector('#trade-table tbody');
    tbody.innerHTML = data.map(t =>
        `<tr>
            <td>${t.created_at || '-'}</td>
            <td>${t.leg1.side.toUpperCase()} @ ${t.leg1.price.toFixed(4)}</td>
            <td>${t.leg2.side.toUpperCase()} @ ${t.leg2.price.toFixed(4)}</td>
            <td>$${t.expected_profit.toFixed(4)}</td>
            <td>${t.status}</td>
        </tr>`
    ).join('');
}

async function refreshFullSignals() {
    const data = await fetchJSON('/api/violations?limit=100');
    const tbody = document.querySelector('#signal-table tbody');
    tbody.innerHTML = data.map(v =>
        `<tr><td>${v.detected_at || '-'}</td><td>${v.price_a?.toFixed(4) ?? '-'}</td><td>${v.price_b?.toFixed(4) ?? '-'}</td><td>${v.violation_amount.toFixed(4)}</td><td>${v.signal}</td></tr>`
    ).join('');
}

// ── Relation Actions ──
async function approveRelation(id) {
    await fetch(`/api/relations/${id}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'approved'})
    });
    refreshRelations(currentRelationFilter);
}

async function rejectRelation(id) {
    await fetch(`/api/relations/${id}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'rejected'})
    });
    refreshRelations(currentRelationFilter);
}

// ── Relation Filter Tabs ──
let currentRelationFilter = 'pending';
document.querySelectorAll('.tabs button').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentRelationFilter = btn.dataset.filter;
        refreshRelations(currentRelationFilter);
    });
});

// ── Refresh Loop ──
async function refreshAll() {
    try {
        await Promise.all([refreshStatus(), refreshStats(), refreshSignals()]);
    } catch (e) {
        document.getElementById('ws-status').textContent = '🔴 断开';
    }
}

refreshAll();
refreshMarkets();
refreshRelations('pending');
refreshTrades();
refreshFullSignals();

setInterval(refreshAll, 2000);
setInterval(refreshMarkets, 10000);
setInterval(() => refreshRelations(currentRelationFilter), 5000);
setInterval(refreshTrades, 5000);
setInterval(refreshFullSignals, 5000);
