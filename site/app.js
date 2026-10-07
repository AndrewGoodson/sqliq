'use strict';
const domains = {
  azure: ['AZURE SPECIALIST', 'Establish the cloud context.', 'Review identity, network isolation, auditing and recovery evidence. Fixed approved reads can inspect server, database and Entra configuration.', 'Microsoft references guide the review. They do not grant permission to run Azure CLI commands.'],
  sql: ['SQL SPECIALIST', 'Understand the database structure.', 'Review schema and index metadata, supplied performance evidence, migration plans and maintenance needs. Produce bounded proposals with compatibility and rollback considerations.', 'No arbitrary SQL or business-row exports. Supported writes need a separate approval bound to the exact plan.'],
  compliance: ['COMPLIANCE SPECIALIST', 'Connect requirements to evidence.', 'Account for imported SQL STIG rules and NIST controls. Record applicability, evidence, findings and recommendations for customer review.', 'An inherited Azure control needs service-specific evidence. Missing evidence never becomes a passing result.']
};
for (const button of document.querySelectorAll('[data-domain]')) {
  button.addEventListener('click', () => {
    for (const peer of document.querySelectorAll('[data-domain]')) {
      const selected = peer === button;
      peer.classList.toggle('selected', selected);
      peer.setAttribute('aria-pressed', String(selected));
    }
    const values = domains[button.dataset.domain];
    ['domain-label', 'domain-title', 'domain-text', 'domain-boundary'].forEach((id, index) => {
      document.getElementById(id).textContent = values[index];
    });
  });
}
