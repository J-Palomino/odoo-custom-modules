/** @odoo-module **/

// Change document title from "Odoo" to "MinTask"
document.addEventListener('DOMContentLoaded', function() {
    // Initial title change
    if (document.title.includes('Odoo')) {
        document.title = document.title.replace('Odoo', 'MinTask');
    } else if (!document.title || document.title === '') {
        document.title = 'MinTask';
    }

    // Watch for title changes and replace Odoo with MinTask
    const observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(mutation) {
            if (document.title.includes('Odoo')) {
                document.title = document.title.replace(/Odoo/g, 'MinTask');
            }
        });
    });

    // Observe the title element
    const titleElement = document.querySelector('title');
    if (titleElement) {
        observer.observe(titleElement, { childList: true, characterData: true, subtree: true });
    }
});

// Also try immediate execution
if (document.title.includes('Odoo')) {
    document.title = document.title.replace(/Odoo/g, 'MinTask');
}
