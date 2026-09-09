/**
 * Theme Manager — Dark/Light Mode Toggle
 * Circular reveal transition using the View Transition API.
 * Falls back to instant swap on unsupported browsers.
 *
 * MUST be loaded in <head> with blocking execution to prevent flash:
 *   <script src="/js/theme.js"></script>
 */
(function () {
    'use strict';

    var STORAGE_KEY = 'theme';
    var DARK = 'dark';
    var LIGHT = 'light';

    function getSavedTheme() {
        try {
            return localStorage.getItem(STORAGE_KEY) || DARK;
        } catch (e) {
            return DARK;
        }
    }

    function applyTheme(theme) {
        if (theme === LIGHT) {
            document.documentElement.setAttribute('data-theme', LIGHT);
        } else {
            document.documentElement.removeAttribute('data-theme');
        }
    }

    function saveTheme(theme) {
        try {
            localStorage.setItem(STORAGE_KEY, theme);
        } catch (e) {}
    }

    // Apply immediately to prevent flash
    applyTheme(getSavedTheme());

    /**
     * Toggle theme with circular reveal animation.
     * @param {MouseEvent|PointerEvent} [event] - click event for coordinates
     */
    window.toggleTheme = function (event) {
        var current = getSavedTheme();
        var next = current === DARK ? LIGHT : DARK;

        // Determine click origin for the circle center
        var x, y;
        if (event && event.clientX !== undefined) {
            x = event.clientX;
            y = event.clientY;
        } else {
            // Fallback: expand from top-right corner (where toggle usually lives)
            x = window.innerWidth - 40;
            y = 40;
        }

        // Calculate max radius so the circle covers the entire viewport
        var endRadius = Math.hypot(
            Math.max(x, window.innerWidth - x),
            Math.max(y, window.innerHeight - y)
        );

        // If View Transition API is not supported, swap instantly
        if (!document.startViewTransition) {
            applyTheme(next);
            saveTheme(next);
            updateToggleButtons(next);
            return next;
        }

        // Start View Transition — the callback applies the DOM change
        var transition = document.startViewTransition(function () {
            applyTheme(next);
            saveTheme(next);
            updateToggleButtons(next);
        });

        // Animate the new view with a circular clip-path reveal
        transition.ready.then(function () {
            document.documentElement.animate(
                {
                    clipPath: [
                        'circle(0px at ' + x + 'px ' + y + 'px)',
                        'circle(' + endRadius + 'px at ' + x + 'px ' + y + 'px)'
                    ]
                },
                {
                    duration: 800,
                    easing: 'cubic-bezier(0.4, 0.0, 0.2, 1)',
                    pseudoElement: '::view-transition-new(root)'
                }
            );
        });

        return next;
    };

    window.getTheme = function () {
        return getSavedTheme();
    };

    function updateToggleButtons(theme) {
        var btn = document.getElementById('themeToggleBtn');
        if (btn) {
            var text = btn.querySelector('.theme-toggle-text');
            var track = btn.querySelector('.theme-switch-track');
            if (text) text.textContent = theme === DARK ? 'Light Mode' : 'Dark Mode';
            if (track) {
                if (theme === LIGHT) {
                    track.classList.add('active');
                } else {
                    track.classList.remove('active');
                }
            }
        }
    }

    window.addEventListener('storage', function (e) {
        if (e.key === STORAGE_KEY && e.newValue) {
            applyTheme(e.newValue);
            updateToggleButtons(e.newValue);
        }
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            updateToggleButtons(getSavedTheme());
        });
    } else {
        updateToggleButtons(getSavedTheme());
    }
})();
