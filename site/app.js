'use strict';
const domains = {
  azure: ['AZURE SPECIALIST', 'Establish the endpoint scope.', 'Review supplied Azure SQL endpoint settings, supported protocols and service-specific assurance. Separate platform responsibilities from customer configuration.', 'Microsoft references guide the review. They do not grant permission to run Azure CLI commands.'],
  sql: ['SQL SPECIALIST', 'Review the actual SQL connection.', 'Review supplied TDS connection evidence, client drivers, certificate validation, trust stores and negotiated cryptography. Track every client path in scope.', 'No arbitrary SQL or business-row exports. Supported writes need a separate approval bound to the exact plan.'],
  compliance: ['COMPLIANCE SPECIALIST', 'Connect requirements to evidence.', 'Map server and client evidence to the 18 SP 800-52 section review items. Record clause applicability, gaps, owners and remediation; keep broader STIG and SP 800-53 reviews separate.', 'An inherited Azure control needs service-specific evidence. Missing evidence never becomes a passing result.']
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
