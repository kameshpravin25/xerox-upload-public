/**
 * Bottom Nav — pill indicator only (no swipe navigation)
 */
(function () {
    'use strict';

    const nav = document.querySelector('.bottom-nav');
    if (!nav) return;

    // --- Nav bar pill indicator ---
    const items = nav.querySelectorAll('.nav-item');
    if (items.length >= 2) {
        const indicator = document.createElement('div');
        indicator.className = 'nav-indicator';
        nav.insertBefore(indicator, nav.firstChild);

        function positionPill() {
            const active = nav.querySelector('.nav-item.active');
            if (!active) return;
            const navR = nav.getBoundingClientRect();
            const actR = active.getBoundingClientRect();
            indicator.style.width = actR.width + 'px';
            indicator.style.height = actR.height + 'px';
            indicator.style.left = (actR.left - navR.left) + 'px';
            indicator.style.top = (actR.top - navR.top) + 'px';
        }

        requestAnimationFrame(positionPill);
        window.addEventListener('resize', positionPill);
    }
})();
