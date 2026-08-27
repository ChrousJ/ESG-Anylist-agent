const chatForm = document.getElementById('chat-form');
const queryInput = document.getElementById('query-input');
const sendBtn = document.getElementById('send-btn');
const chatContainer = document.getElementById('chat-container');

// Expected nodes in language graph pipeline
const PIPELINE_NODES = [
    'context', 'supervisor', 'schema_injector', 
    'sql_worker', 'rag_worker', 'worker_aggregator', 
    'evaluator_d', 'map_reduce', 'synthesizer',
    'disclosure_scorer', 'greenwashing_detector',
    'evaluator_o', 'memory_updater'
];

// Configure markdown parser
marked.setOptions({
    gfm: true,
    breaks: true
});

// Auto-resize textarea
queryInput.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = (this.scrollHeight) + 'px';
    sendBtn.disabled = this.value.trim() === '';
});

// Allow Enter to send (Shift+Enter for newline)
queryInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (!sendBtn.disabled) {
            chatForm.dispatchEvent(new Event('submit'));
        }
    }
});

// Set input from chips
window.setInput = (text) => {
    queryInput.value = text;
    queryInput.dispatchEvent(new Event('input'));
    queryInput.focus();
};

// Create a message bubble
function createMessageBubble(role, contentHTML = '') {
    const div = document.createElement('div');
    div.className = `message ${role}-message`;
    
    // Avatar
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    if (role === 'user') {
        avatar.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>`;
    } else {
        avatar.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M12 2L15 9L22 12L15 15L12 22L9 15L2 12L9 9L12 2Z" fill="currentColor"/></svg>`;
    }
    
    // Content
    const content = document.createElement('div');
    content.className = 'message-content';
    if (contentHTML) content.innerHTML = contentHTML;
    
    div.appendChild(avatar);
    div.appendChild(content);
    chatContainer.appendChild(div);
    
    // Smooth scroll down
    chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });
    
    return content;
}

// Create progress tracker UI
function createTracker() {
    const html = `
        <div class="tracker-container">
            <div class="tracker-header">
                <span>Agent Pipeline (Node Progress Stream)</span>
                <span class="trace-id-badge" id="current-trace">...</span>
            </div>
            <div class="nodes-timeline" id="nodes-timeline">
                ${PIPELINE_NODES.map(n => `
                    <div class="node-badge" id="node-${n}">
                        <div class="spinner"></div>
                        <span class="node-name">${n}</span>
                        <span class="time-ms"></span>
                    </div>
                `).join('')}
            </div>
        </div>
        <div id="analysis-content" class="analysis-content">
            <p>思考中 <span class="loading-dots">...</span></p>
        </div>
    `;
    return html;
}

function updateNodeStatus(nodeName, duration, status) {
    const el = document.getElementById(`node-${nodeName}`);
    if (el) {
        el.className = `node-badge ${status}`;
        if (duration > 0) {
            el.querySelector('.time-ms').textContent = `${duration}ms`;
        }
    }
}

function markActiveNode(nodeName) {
    // For visual effect, if we receive a node complete, we mark the next logical node as running
    const idx = PIPELINE_NODES.indexOf(nodeName);
    if (idx !== -1 && idx + 1 < PIPELINE_NODES.length) {
        const nextEl = document.getElementById(`node-${PIPELINE_NODES[idx+1]}`);
        if (nextEl && (nextEl.className.includes('pending') || !nextEl.className.includes('success'))) {
            nextEl.className = 'node-badge running';
        }
    }
}

// Render Plotly Chart
function renderChart(containerId, spec) {
    if (!spec || !spec.series || spec.series.length === 0) return;
    
    const container = document.getElementById(containerId);
    if (!container) return;
    
    const traces = spec.series.map(s => {
        return {
            x: spec.x_axis,
            y: s.data,
            name: s.name,
            type: spec.type === 'line' ? 'scatter' : 'bar',
            mode: spec.type === 'line' ? 'lines+markers' : 'none',
            hoverinfo: 'x+y+name',
        };
    });
    
    const layout = {
        title: spec.title,
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: { color: '#e6edf3' },
        xaxis: { gridcolor: '#30363d' },
        yaxis: { gridcolor: '#30363d' },
        margin: { l: 40, r: 20, t: 40, b: 30 },
        legend: { orientation: 'h', y: -0.2 }
    };
    
    Plotly.newPlot(containerId, traces, layout, {responsive: true, displayModeBar: false});
}

// Handle Form Submission
chatForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const query = queryInput.value.trim();
    if (!query) return;
    
    // UI Updates
    queryInput.value = '';
    queryInput.style.height = 'auto';
    sendBtn.disabled = true;
    
    createMessageBubble('user', `<p>${query}</p>`);
    
    const agentContent = createMessageBubble('system', createTracker());
    
    // Start Request
    try {
        const response = await fetch('/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: query, stream: true })
        });
        
        if (!response.ok) {
            agentContent.innerHTML = `<p style="color:var(--node-failed)">Request failed. Please retry with a narrower query.</p>`;
            sendBtn.disabled = false;
            return;
        }
        
        // Setup initially active node
        updateNodeStatus('context', 0, 'running');
        
        // Read SSE stream
        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop(); // keep remainder
            
            for (const line of lines) {
                if (line.trim() === '') continue;
                if (line.startsWith('data: ')) {
                    const dataStr = line.substring(6).trim();
                    try {
                        const data = JSON.parse(dataStr);
                        
                        if (data.event === 'trace_id') {
                            document.getElementById('current-trace').textContent = data.trace_id.substring(0,8);
                            document.getElementById('system-status').textContent = 'Processing...';
                            document.getElementById('system-status').style.color = 'var(--node-running)';
                        }
                        else if (data.event === 'node_complete') {
                            updateNodeStatus(data.node, data.duration_ms, data.status === 'ok' ? 'success' : 'failed');
                            markActiveNode(data.node);
                        }
                        else if (data.event === 'analysis_complete') {
                            // Final Render
                            let finalHtml = `
                                <div class="markdown-body">
                                    ${marked.parse(data.analysis)}
                                </div>
                            `;
                            
                            // Key Findings
                            if (data.key_findings && data.key_findings.length > 0) {
                                finalHtml += `
                                    <div class="key-findings" style="margin-top:1.5rem; padding:1rem; background:rgba(35,134,54,0.1); border-radius:8px; border-left:3px solid #3fb950;">
                                        <h4 style="margin-bottom:0.5rem; color:#3fb950;">核心摘要</h4>
                                        <ul style="margin:0; padding-left:1.5rem; font-size:0.9rem;">
                                            ${data.key_findings.map(f => `<li>${f}</li>`).join('')}
                                        </ul>
                                    </div>
                                `;
                            }
                            
                            // Disclosure Quality Score
                            if (data.disclosure_quality) {
                                const dq = data.disclosure_quality;
                                const riskCount = (dq.risk_flags || []).length;
                                finalHtml += `
                                    <div class="dq-card">
                                        <div class="dq-score">${dq.score}<span>/100</span></div>
                                        <div>
                                            <h4>披露质量评分 · ${dq.band}</h4>
                                            <p>确定性评分 Rubric：完整性、连续性、可比性、可验证性、具体性。</p>
                                            <p class="dq-risk">风险信号：${riskCount} 个</p>
                                        </div>
                                    </div>
                                `;
                            }

                            // Greenwashing Risk Radar
                            if (data.greenwashing_risks) {
                                const gw = data.greenwashing_risks;
                                const count = gw.risk_count || 0;
                                finalHtml += `
                                    <div class="gw-card ${count > 0 ? 'has-risk' : ''}">
                                        <div class="gw-count">${count}</div>
                                        <div>
                                            <h4>潜在绿漂风险雷达</h4>
                                            <p>${gw.summary || '当前证据未触发风险信号'}</p>
                                            <p class="gw-method">claim-evidence mismatch · 规则型检测</p>
                                        </div>
                                    </div>
                                `;
                            }

                            // Chart Area
                            const chartId = `chart-${Date.now()}`;
                            if (data.chart_spec) {
                                finalHtml += `<div id="${chartId}" class="chart-container"></div>`;
                            }

                            // Sources
                            if (data.sources && data.sources.length > 0) {
                                finalHtml += `
                                    <div class="sources-section">
                                        <h4>Evidence Sources (${data.sources.length})</h4>
                                        ${data.sources.map(s => {
                                            if (s.type === 'sql') {
                                                return `<div class="source-item sql"><strong>[SQL]</strong> Structured query evidence captured.</div>`;
                                            }
                                            const excerpt = s.excerpt ? `<div class="source-excerpt">${s.excerpt}</div>` : '';
                                            return `<div class="source-item rag"><strong>[PDF]</strong> ${s.company} ${s.year}, p.${s.page} (score ${(s.score).toFixed(2)})${excerpt}</div>`;
                                        }).join('')}
                                    </div>
                                `;
                            }
                            
                            document.getElementById('analysis-content').innerHTML = finalHtml;
                            
                            if (data.chart_spec) {
                                renderChart(chartId, data.chart_spec);
                            }
                            
                            // Finish all nodes visually
                            PIPELINE_NODES.forEach(n => {
                                const el = document.getElementById(`node-${n}`);
                                if (el && el.className.includes('running')) {
                                    el.className = 'node-badge success';
                                }
                            });
                            
                            document.getElementById('system-status').textContent = 'Ready';
                            document.getElementById('system-status').style.color = '#3fb950';
                        }
                        else if (data.event === 'error') {
                            document.getElementById('analysis-content').innerHTML = `<p style="color:var(--node-failed)">${data.message}</p>`;
                            document.getElementById('system-status').textContent = 'Error';
                            document.getElementById('system-status').style.color = 'var(--node-failed)';
                        }
                        
                    } catch (e) {
                        console.error('SSE JSON parse error:', e, dataStr);
                    }
                }
            }
        }
    } catch (err) {
        agentContent.innerHTML += `<p style="color:var(--node-failed)">Fetch Error: ${err.message}</p>`;
    } finally {
        sendBtn.disabled = queryInput.value.trim() === '';
        queryInput.focus();
        chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });
    }
});

