'use strict';
const descriptions = {
  orchestrator: ['ORCHESTRATOR', 'Builds an offline assessment through the AEF graph. Binds the plan to reviewed source hashes and a specific policy. Holds no database credentials.'],
  azure: ['AZURE SPECIALIST', 'Identifies identity, network, audit and recovery controls for review. Approved live reads check public network access, minimum TLS, Entra-only authentication and database status. Deployment evidence remains separate.'],
  sql: ['SQL SPECIALIST', 'Plans fixed schema or index catalog reads. Produces bounded metadata assessments and offline schema-change proposals. Cannot execute arbitrary SQL or access business rows.'],
  compliance: ['COMPLIANCE SPECIALIST', 'Accounts for every rule in a supplied DISA SQL STIG benchmark. Reviews NIST TLS evidence and financial control applicability. Missing evidence remains unassessed; no automatic compliance or certification claim.'],
  broker: ['HUMAN APPROVAL BOUNDARY', 'Verifies an external Ed25519 signature, exact plan and trusted policy. Reserves a single-use nonce before any credential or network access. A read approval cannot authorize a write.']
};
for (const node of document.querySelectorAll('[data-node]')) {
  node.addEventListener('click', () => {
    for (const sibling of document.querySelectorAll('[data-node]')) {
      sibling.classList.toggle('active', sibling === node);
      sibling.setAttribute('aria-pressed', String(sibling === node));
    }
    const [label, text] = descriptions[node.dataset.node];
    document.getElementById('detail-label').textContent = label;
    document.getElementById('detail-text').textContent = text;
  });
}
