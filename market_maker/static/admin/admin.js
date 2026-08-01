// Admin UI JavaScript — Limit Order Book Demo Admin Control

(function() {
  'use strict';

  // Confirmation dialog management
  const confirmDialog = document.createElement('div');
  confirmDialog.className = 'confirm-dialog';
  confirmDialog.setAttribute('role', 'dialog');
  confirmDialog.setAttribute('aria-modal', 'true');
  confirmDialog.setAttribute('aria-labelledby', 'confirm-title');
  confirmDialog.innerHTML = `
    <div class="confirm-dialog__content">
      <div class="confirm-dialog__header">
        <h3 id="confirm-title" class="confirm-dialog__title">Confirm Action</h3>
        <button type="button" class="confirm-dialog__close" aria-label="Close">&times;</button>
      </div>
      <div class="confirm-dialog__body">
        <div class="confirm-dialog__warning" id="confirm-warning"></div>
        <p id="confirm-message"></p>
        <div class="confirm-dialog__fields" id="confirm-fields"></div>
      </div>
      <div class="confirm-dialog__footer">
        <button type="button" class="btn btn-secondary" id="confirm-cancel">Cancel</button>
        <button type="button" class="btn btn-primary" id="confirm-ok">Confirm</button>
      </div>
    </div>
  `;
  document.body.appendChild(confirmDialog);

  let currentForm = null;
  let currentConfirmInput = null;

  function showConfirmDialog(form, message, warningType = 'warning') {
    currentForm = form;
    const warningEl = confirmDialog.querySelector('#confirm-warning');
    const messageEl = confirmDialog.querySelector('#confirm-message');
    const fieldsEl = confirmDialog.querySelector('#confirm-fields');

    messageEl.textContent = message;
    warningEl.textContent = warningType === 'danger'
      ? '⚠️ DESTRUCTIVE ACTION: This cannot be undone without a backup restore.'
      : '⚠️ This action will modify the demo state.';
    warningEl.className = 'confirm-dialog__warning' + (warningType === 'danger' ? ' danger' : '');

    // Check if form has a confirm input field
    const confirmInput = form.querySelector('input[name="confirm"]');
    if (confirmInput) {
      currentConfirmInput = confirmInput;
      fieldsEl.innerHTML = `
        <div class="form-group">
          <label for="confirm-dialog-input" class="form-label">Type the confirmation word exactly:</label>
          <input type="text" id="confirm-dialog-input" class="form-input" autocomplete="off" required>
        </div>
      `;
      const dialogInput = document.getElementById('confirm-dialog-input');
      dialogInput.focus();
      dialogInput.addEventListener('input', function() {
        confirmInput.value = this.value;
        document.getElementById('confirm-ok').disabled = this.value !== confirmInput.placeholder;
      });
      document.getElementById('confirm-ok').disabled = true;
    } else {
      currentConfirmInput = null;
      fieldsEl.innerHTML = '';
      document.getElementById('confirm-ok').disabled = false;
    }

    confirmDialog.classList.add('open');
    document.body.style.overflow = 'hidden';
  }

  function hideConfirmDialog() {
    confirmDialog.classList.remove('open');
    document.body.style.overflow = '';
    currentForm = null;
    currentConfirmInput = null;
  }

  confirmDialog.querySelector('#confirm-cancel').addEventListener('click', hideConfirmDialog);
  confirmDialog.querySelector('#confirm-close').addEventListener('click', hideConfirmDialog);
  confirmDialog.querySelector('#confirm-ok').addEventListener('click', function() {
    if (currentForm) {
      currentForm.submit();
    }
    hideConfirmDialog();
  });

  // Close on backdrop click
  confirmDialog.addEventListener('click', function(e) {
    if (e.target === confirmDialog) {
      hideConfirmDialog();
    }
  });

  // Close on Escape key
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && confirmDialog.classList.contains('open')) {
      hideConfirmDialog();
    }
  });

  // Attach confirmation to forms with data-confirm attribute
  function attachConfirmListeners() {
    document.querySelectorAll('form[data-confirm]').forEach(function(form) {
      // Remove any existing listener to avoid duplicates
      form.removeEventListener('submit', handleFormSubmit);
      form.addEventListener('submit', handleFormSubmit);
    });
  }

  function handleFormSubmit(e) {
    const confirmMessage = this.getAttribute('data-confirm');
    const isDangerous = confirmMessage.includes('RESET') || confirmMessage.includes('RESTORE');
    showConfirmDialog(this, confirmMessage, isDangerous ? 'danger' : 'warning');
    e.preventDefault();
  }

  // Loading overlay
  const loadingOverlay = document.createElement('div');
  loadingOverlay.className = 'loading-overlay';
  loadingOverlay.innerHTML = '<div class="loading-spinner" aria-label="Loading..."></div>';
  document.body.appendChild(loadingOverlay);

  function showLoading() {
    loadingOverlay.classList.add('open');
  }

  function hideLoading() {
    loadingOverlay.classList.remove('open');
  }

  // Show loading on form submit (for non-confirmation forms)
  document.addEventListener('submit', function(e) {
    const form = e.target;
    if (form.tagName === 'FORM' && !form.hasAttribute('data-confirm') && form.method.toUpperCase() === 'POST') {
      // Don't show loading for forms that open in new window (like export)
      if (!form.target || form.target === '_self') {
        showLoading();
      }
    }
  });

  // Hide loading on page load
  window.addEventListener('load', hideLoading);

  // Handle page navigation (back/forward)
  window.addEventListener('pageshow', function(e) {
    if (e.persisted) {
      hideLoading();
    }
  });

  // Initialize on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attachConfirmListeners);
  } else {
    attachConfirmListeners();
  }

  // Re-attach on dynamic content (for any future AJAX)
  const observer = new MutationObserver(function(mutations) {
    let shouldAttach = false;
    mutations.forEach(function(mutation) {
      if (mutation.addedNodes.length) {
        shouldAttach = true;
      }
    });
    if (shouldAttach) {
      attachConfirmListeners();
    }
  });

  observer.observe(document.body, { childList: true, subtree: true });

  // Export for testing
  window.AdminUI = {
    showConfirmDialog,
    hideConfirmDialog,
    showLoading,
    hideLoading,
    attachConfirmListeners
  };
})();