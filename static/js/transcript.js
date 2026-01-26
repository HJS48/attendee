// Transcript Viewer JavaScript

(function() {
    'use strict';

    // Elements
    const video = document.getElementById('video-player');
    const transcriptContainer = document.getElementById('transcript-container');
    const copyBtn = document.getElementById('copy-transcript-btn');
    const copyActionsBtn = document.getElementById('copy-actions-btn');
    const copySummaryBtn = document.getElementById('copy-summary-btn');
    const segments = document.querySelectorAll('.transcript-segment');

    // State
    let isUserScrolling = false;
    let scrollTimeout = null;

    // Initialize
    function init() {
        if (!video || !transcriptContainer) return;

        // Video time update - highlight current segment
        video.addEventListener('timeupdate', handleTimeUpdate);

        // Click on segment to seek
        segments.forEach(segment => {
            segment.addEventListener('click', handleSegmentClick);
        });

        // Copy transcript button
        if (copyBtn) {
            copyBtn.addEventListener('click', handleCopyTranscript);
        }

        // Copy action items button
        if (copyActionsBtn) {
            copyActionsBtn.addEventListener('click', handleCopyActions);
        }

        // Copy summary button
        if (copySummaryBtn) {
            copySummaryBtn.addEventListener('click', handleCopySummary);
        }

        // Track user scrolling to pause auto-scroll temporarily
        transcriptContainer.addEventListener('scroll', handleUserScroll);

        // Click on action item timestamp to jump to transcript
        document.querySelectorAll('.ai-timestamp').forEach(ts => {
            ts.addEventListener('click', handleTimestampClick);
        });
    }

    // Handle click on action item timestamp
    function handleTimestampClick(e) {
        e.preventDefault();
        const timestampStr = e.currentTarget.dataset.timestamp;
        if (!timestampStr) return;

        // Parse timestamp like "[23:21]" or "23:21"
        const match = timestampStr.match(/(\d+):(\d+)/);
        if (!match) return;

        const minutes = parseInt(match[1], 10);
        const seconds = parseInt(match[2], 10);
        const targetMs = (minutes * 60 + seconds) * 1000;

        // Find the closest transcript segment
        let closestSegment = null;
        let closestDiff = Infinity;

        segments.forEach(segment => {
            const startMs = parseInt(segment.dataset.start, 10);
            const diff = Math.abs(startMs - targetMs);
            if (diff < closestDiff) {
                closestDiff = diff;
                closestSegment = segment;
            }
        });

        if (closestSegment) {
            // Remove active from all, add to target
            segments.forEach(s => s.classList.remove('active'));
            closestSegment.classList.add('active');

            // Scroll to segment
            closestSegment.scrollIntoView({ behavior: 'smooth', block: 'center' });

            // Also seek video to that point
            video.currentTime = targetMs / 1000;
        }
    }

    // Handle video time update
    function handleTimeUpdate() {
        const currentTimeMs = video.currentTime * 1000;
        let activeSegment = null;

        segments.forEach(segment => {
            const startMs = parseInt(segment.dataset.start, 10);
            const endMs = parseInt(segment.dataset.end, 10);

            if (currentTimeMs >= startMs && currentTimeMs < endMs) {
                segment.classList.add('active');
                activeSegment = segment;
            } else {
                segment.classList.remove('active');
            }
        });

        // Auto-scroll to active segment (unless user is manually scrolling)
        if (activeSegment && !isUserScrolling) {
            scrollToSegment(activeSegment);
        }
    }

    // Scroll transcript to show active segment
    function scrollToSegment(segment) {
        const container = transcriptContainer;
        const segmentTop = segment.offsetTop - container.offsetTop;
        const segmentHeight = segment.offsetHeight;
        const containerHeight = container.clientHeight;
        const scrollTop = container.scrollTop;

        // Only scroll if segment is not visible
        if (segmentTop < scrollTop || segmentTop + segmentHeight > scrollTop + containerHeight) {
            container.scrollTo({
                top: segmentTop - containerHeight / 3,
                behavior: 'smooth'
            });
        }
    }

    // Handle click on transcript segment
    function handleSegmentClick(e) {
        const segment = e.currentTarget;
        const startMs = parseInt(segment.dataset.start, 10);
        const startSec = startMs / 1000;

        video.currentTime = startSec;
        video.play();
    }

    // Handle user manually scrolling
    function handleUserScroll() {
        isUserScrolling = true;

        // Reset after 3 seconds of no scrolling
        if (scrollTimeout) {
            clearTimeout(scrollTimeout);
        }
        scrollTimeout = setTimeout(() => {
            isUserScrolling = false;
        }, 3000);
    }

    // Copy transcript to clipboard
    function handleCopyTranscript() {
        const segments = document.querySelectorAll('.transcript-segment');
        let transcriptText = '';

        segments.forEach(segment => {
            const speaker = segment.querySelector('.speaker-name').textContent;
            const time = segment.querySelector('.segment-time').textContent;
            const text = segment.querySelector('.segment-text').textContent.trim();
            transcriptText += `${speaker} ${time}\n${text}\n\n`;
        });

        navigator.clipboard.writeText(transcriptText.trim()).then(() => {
            // Show success feedback
            const originalText = copyBtn.querySelector('.copy-text').textContent;
            copyBtn.querySelector('.copy-text').textContent = 'Copied!';
            copyBtn.classList.add('copied');

            setTimeout(() => {
                copyBtn.querySelector('.copy-text').textContent = originalText;
                copyBtn.classList.remove('copied');
            }, 2000);
        }).catch(err => {
            console.error('Failed to copy transcript:', err);
            alert('Failed to copy transcript to clipboard');
        });
    }

    // Copy summary to clipboard
    function handleCopySummary() {
        const summaryText = document.querySelector('.summary-text');
        if (!summaryText) return;

        navigator.clipboard.writeText(summaryText.textContent.trim()).then(() => {
            copySummaryBtn.querySelector('.copy-text').textContent = 'Copied!';
            copySummaryBtn.classList.add('copied');
            setTimeout(() => {
                copySummaryBtn.querySelector('.copy-text').textContent = 'Copy';
                copySummaryBtn.classList.remove('copied');
            }, 2000);
        }).catch(err => {
            console.error('Failed to copy summary:', err);
        });
    }

    // Copy action items to clipboard
    function handleCopyActions() {
        const groups = document.querySelectorAll('.ai-group');
        let text = '';

        groups.forEach(group => {
            const name = group.querySelector('.ai-assignee-header').textContent.replace(/\d+$/, '').trim();
            const items = group.querySelectorAll('.ai-row');
            text += `${name}:\n`;
            items.forEach(item => {
                const task = item.querySelector('.ai-task').textContent.trim();
                const due = item.querySelector('.ai-due');
                text += `  ☐ ${task}${due ? ' (' + due.textContent + ')' : ''}\n`;
            });
            text += '\n';
        });

        navigator.clipboard.writeText(text.trim()).then(() => {
            copyActionsBtn.querySelector('.copy-text').textContent = 'Copied!';
            copyActionsBtn.classList.add('copied');
            setTimeout(() => {
                copyActionsBtn.querySelector('.copy-text').textContent = 'Copy';
                copyActionsBtn.classList.remove('copied');
            }, 2000);
        }).catch(err => {
            console.error('Failed to copy action items:', err);
        });
    }

    // Escape HTML for XSS prevention
    function escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = String(str);
        return div.innerHTML;
    }

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
