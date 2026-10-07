'use strict';
const domains = {
  azure: ['AZURE SPECIALIST', 'Define the source and target.',
    'Review the Azure service model, migration path, identity, networking, encryption and recovery requirements. Record which controls need Azure assurance and which remain with the customer.',
    'Microsoft guidance informs the plan. Missing source or target evidence stays unassessed; live Azure access still requires exact signed approval.'],
  sql: ['SQL SPECIALIST', 'Protect integrity through migration.',
    'Review dependencies, schema compatibility, financial reconciliation, performance baselines and restore evidence. Define cutover criteria, rollback triggers and post-migration validation.',
    'The migration skill prepares a reviewed plan. It cannot execute a migration, arbitrary SQL or a cutover. Supported broker writes require separate exact-plan approval.'],
  compliance: ['COMPLIANCE SPECIALIST', 'Keep every STIG rule in view.',
    'Carry all 102 pinned SQL Server STIG rules into source and target review. Track applicability, evidence, findings, remediation owners and exceptions. Review SP 800-53 controls and SP 800-52 TLS separately.',
    'Hooks require the offline review contract to retain its checks. Azure inheritance and missing evidence never become automatic passes; financial applicability needs organization-specific review.']
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
