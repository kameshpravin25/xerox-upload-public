/**
 * Frontend Security — DevTools & Inspect Protection
 * Blocks common methods of opening DevTools and viewing page source.
 * Include this script in customer-facing pages (index, login, history).
 */
(function () {
    'use strict';

    // ========== 1. Disable Right-Click Context Menu ==========
    document.addEventListener('contextmenu', function (e) {
        e.preventDefault();
        return false;
    });

    // ========== 2. Block Keyboard Shortcuts ==========
    document.addEventListener('keydown', function (e) {
        // F12 — DevTools
        if (e.key === 'F12' || e.keyCode === 123) {
            e.preventDefault();
            e.stopPropagation();
            return false;
        }

        // Ctrl+Shift+I — DevTools
        if (e.ctrlKey && e.shiftKey && (e.key === 'I' || e.key === 'i')) {
            e.preventDefault();
            e.stopPropagation();
            return false;
        }

        // Ctrl+Shift+J — Console
        if (e.ctrlKey && e.shiftKey && (e.key === 'J' || e.key === 'j')) {
            e.preventDefault();
            e.stopPropagation();
            return false;
        }

        // Ctrl+Shift+C — Element Picker
        if (e.ctrlKey && e.shiftKey && (e.key === 'C' || e.key === 'c')) {
            e.preventDefault();
            e.stopPropagation();
            return false;
        }

        // Ctrl+U — View Source
        if (e.ctrlKey && (e.key === 'U' || e.key === 'u')) {
            e.preventDefault();
            e.stopPropagation();
            return false;
        }

        // Ctrl+S — Save Page
        if (e.ctrlKey && (e.key === 'S' || e.key === 's')) {
            e.preventDefault();
            e.stopPropagation();
            return false;
        }

        // Ctrl+Shift+K — Firefox Console
        if (e.ctrlKey && e.shiftKey && (e.key === 'K' || e.key === 'k')) {
            e.preventDefault();
            e.stopPropagation();
            return false;
        }

        // Ctrl+Shift+M — Responsive Design Mode (Firefox)
        if (e.ctrlKey && e.shiftKey && (e.key === 'M' || e.key === 'm')) {
            e.preventDefault();
            e.stopPropagation();
            return false;
        }
    });

    // ========== 3. Disable Text Selection ==========
    document.addEventListener('selectstart', function (e) {
        // Allow selection in input fields and textareas
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) {
            return true;
        }
        e.preventDefault();
        return false;
    });

    // ========== 6. Disable Drag ==========
    document.addEventListener('dragstart', function (e) {
        e.preventDefault();
        return false;
    });

    // ========== 7. Block Copy (except in input fields) ==========
    document.addEventListener('copy', function (e) {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
            return true;
        }
        e.preventDefault();
        return false;
    });

    // ========== 8. Clear Console ==========
    if (typeof console.clear === 'function') {
        console.clear();
    }
    console.log(
        '%c⚠️ STOP!',
        'color:red;font-size:40px;font-weight:bold;'
    );
    console.log(
        '%cThis browser feature is for developers. Do not paste any code here.',
        'color:white;font-size:16px;'
    );

})();
