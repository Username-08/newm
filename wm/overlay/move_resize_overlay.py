from threading import Thread
import time
import logging

from pywm import PYWM_PRESSED

from pywm.touchpad import (
    SingleFingerMoveGesture,
    TwoFingerSwipePinchGesture,
    GestureListener,
    LowpassGesture
)
from .overlay import Overlay
from ..grid import Grid

GRID_OVR = 0.2
GRID_M = 2

class MoveOverlay:
    def __init__(self, layout, view):
        self.layout = layout

        self.view = view
        self.i = 0
        self.j = 0

        try:
            view_state = self.layout.state.get_view_state(self.view)
            self.i = view_state.i
            self.j = view_state.j

            self.layout.update(
                self.layout.state.replacing_view_state(
                    self.view,
                    move_origin=(self.i, self.j)
                ))
        except Exception:
            logging.warn("Unexpected: Could not access view %s state", self.view)

        self.i_grid = Grid("i", self.i - 3, self.i + 3, self.i, GRID_OVR, GRID_M)
        self.j_grid = Grid("j", self.j - 3, self.j + 3, self.j, GRID_OVR, GRID_M)

        self.last_dx = 0
        self.last_dy = 0

        self._closed = False

    def reset_gesture(self):
        self.last_dx = 0
        self.last_dy = 0

    def on_gesture(self, values):
        if self._closed:
            return
        
        self.i += 4*(values['delta_x'] - self.last_dx)
        self.j += 4*(values['delta_y'] - self.last_dy)
        self.last_dx = values['delta_x']
        self.last_dy = values['delta_y']
        self.layout.state.update_view_state(
            self.view, i=self.i_grid.at(self.i), j=self.j_grid.at(self.j))
        self.layout.damage()

    def close(self):
        self._closed = True

        try:
            state = self.layout.state.get_view_state(self.view)
            fi, ti = self.i_grid.final(restrict_by_x_current=True)
            fj, tj = self.j_grid.final(restrict_by_x_current=True)

            logging.debug("Move - Grid finals: %f %f (%f %f)", fi, fj, ti, tj)

            return state.i, state.j, state.w, state.h, fi, fj, state.w, state.h, max(ti, tj)
        except Exception:
            logging.warn("Unexpected: Could not access view %s state... returning default placement", self.view)
            return self.i, self.j, 1, 1, round(self.i), round(self.j), 1, 1, 1


class ResizeOverlay:
    def __init__(self, layout, view):
        self.layout = layout
        self.view = view

        self.i = 0
        self.j = 0
        self.w = 1
        self.h = 1

        try:
            view_state = self.layout.state.get_view_state(self.view)
            self.i = view_state.i
            self.j = view_state.j
            self.w = view_state.w
            self.h = view_state.h

            self.layout.update(
                self.layout.state.replacing_view_state(
                    self.view,
                    move_origin=(view_state.i, view_state.j),
                    scale_origin=(view_state.w, view_state.h)
                ))
        except Exception:
            logging.warn("Unexpected: Could not access view %s state", self.view)


        self.i_grid = Grid("i", self.i - 3, self.i + 3, self.i, GRID_OVR, GRID_M)
        self.j_grid = Grid("j", self.j - 3, self.j + 3, self.j, GRID_OVR, GRID_M)
        self.w_grid = Grid("w", 1, self.w + 3, self.w, GRID_OVR, GRID_M)
        self.h_grid = Grid("h", 1, self.h + 3, self.h, GRID_OVR, GRID_M)

        self._closed = False

    def on_gesture(self, values):
        if self._closed:
            return

        dw = 4*values['delta_x']
        dh = 4*values['delta_y']

        i, j, w, h = self.i, self.j, self.w, self.h

        if self.w + dw < 1:
            d = 1 - (self.w + dw)
            i = self.i - d
            w = 1 + d
        else:
            i = self.i
            w = self.w + dw

        if self.h + dh < 1:
            d = 1 - (self.h + dh)
            j = self.j - d
            h = 1 + d
        else:
            j = self.j
            h = self.h + dh

        self.layout.state.update_view_state(
            self.view, i=self.i_grid.at(i), j=self.j_grid.at(j), w=self.w_grid.at(w), h=self.h_grid.at(h))

        self.layout.damage()

    def close(self):
        self._closed = True

        try:
            state = self.layout.state.get_view_state(self.view)
            fi, ti = self.i_grid.final(restrict_by_x_current=True)
            fj, tj = self.j_grid.final(restrict_by_x_current=True)
            fw, tw = self.w_grid.final(restrict_by_x_current=True)
            fh, th = self.h_grid.final(restrict_by_x_current=True)

            logging.debug("Resize - Grid finals: %f %f %f %f (%f %f %f %f)", fi, fj, fw, fh, ti, tj, tw, th)

            return state.i, state.j, state.w, state.h, fi, fj, fw, fh, max(ti, tj, tw, th)
        except Exception:
            logging.warn("Unexpected: Could not access view %s state... returning default placement", self.view)
            return self.i, self.j, self.w, self.h, self.i, self.j, self.w, self.h, 1



class MoveResizeOverlay(Overlay, Thread):
    def __init__(self, layout, view):
        Overlay.__init__(self, layout)
        Thread.__init__(self)

        self.layout.update_cursor(False)

        self.view = view

        self.overlay = None

        """
        If move has been finished and we are animating towards final position
            (view initial i, view initial j, view final i, view final j, initial time, finished time)
        """
        self._target_view_pos = None

        """
        If resize has been finished and we are animating towards final size
            (view initial w, view initial h, view final w, view final h, initial time, finished time)
        """
        self._target_view_size = None

        """
        If we are adjusting viewpoint (after gesture finished or during)
            (layout initial i, layout initial j, layout final i, layout final j, initial time, finished time)
        """
        self._target_layout_pos = None
        
        self._running = True
        self._wants_close = False

    def post_init(self):
        logging.debug("MoveResizeOverlay: Starting thread...")
        self.start()

    def run(self):
        while self._running:
            t = time.time()

            in_prog = False
            if self._target_view_pos is not None:
                in_prog = True
                ii, ij, fi, fj, it, ft = self._target_view_pos
                if t > ft:
                    self.layout.state.update_view_state(self.view, i=fi, j=fj)
                    self._target_view_pos = None
                else:
                    perc = (t-it)/(ft-it)
                    self.layout.state.update_view_state(self.view, i=ii + perc*(fi-ii), j=ij + perc*(fj-ij))
                self.layout.damage()


            if self._target_view_size is not None:
                in_prog = True
                iw, ih, fw, fh, it, ft = self._target_view_size
                if t > ft:
                    self.layout.state.update_view_state(self.view, w=fw, h=fh, scale_origin=(None, None))
                    self._target_view_size = None
                else:
                    perc = (t-it)/(ft-it)
                    self.layout.state.update_view_state(self.view, w=iw + perc*(fw-iw), h=ih + perc*(fh-ih))
                self.layout.damage()

            if self._target_layout_pos is not None:
                in_prog = True
                ii, ij, fi, fj, it, ft = self._target_layout_pos
                if t > ft:
                    self.layout.state.i = fi
                    self.layout.state.j = fj
                    self._target_layout_pos = None
                else:
                    perc = (t-it)/(ft-it)
                    self.layout.state.i=ii + perc*(fi-ii)
                    self.layout.state.j=ij + perc*(fj-ij)
                self.layout.damage()

            elif self.overlay is not None:
                try:
                    view_state = self.layout.state.get_view_state(self.view)
                    i, j, w, h = view_state.i, view_state.j, view_state.w, view_state.h
                    i, j, w, h = round(i), round(j), round(w), round(h)

                    fi, fj = self.layout.state.i, self.layout.state.j

                    if i + w > fi + self.layout.state.size:
                        fi = i + w - self.layout.state.size

                    if j + h > fj + self.layout.state.size:
                        fj = j + h - self.layout.state.size

                    if i < fi:
                        fi = i

                    if j < fj:
                        fj = j

                    if i != self.layout.state.i or j != self.layout.state.j:
                        logging.debug("MoveResizeOverlay: Adjusting viewpoint")
                        self._target_layout_pos = (self.layout.state.i, self.layout.state.j, fi, fj, time.time(), time.time() + .3)

                except Exception:
                    logging.warn("Unexpected: Could not access view %s state", self.view)


            if not in_prog and self._wants_close:
                self._running = False

            time.sleep(1. / 120.)

        logging.debug("MoveResizeOverlay: Thread finished")
        self.layout.exit_overlay()

    def on_gesture(self, gesture):
        if not self._running or self._wants_close:
            logging.debug("MoveResizeOverlay: Rejecting gesture")
            return

        if isinstance(gesture, TwoFingerSwipePinchGesture):
            logging.debug("MoveResizeOverlay: New TwoFingerSwipePinch")
            self._target_view_pos = None
            self._target_view_size = None

            self.overlay = ResizeOverlay(self.layout, self.view)
            LowpassGesture(gesture).listener(GestureListener(
                self.overlay.on_gesture,
                self.finish
            ))
            return True

        if isinstance(gesture, SingleFingerMoveGesture):
            logging.debug("MoveResizeOverlay: New SingleFingerMove")
            self._target_view_pos = None

            self.overlay = MoveOverlay(self.layout, self.view)
            LowpassGesture(gesture).listener(GestureListener(
                self.overlay.on_gesture,
                self.finish
            ))
            return True

        return False


    def finish(self):
        logging.debug("MoveResizeOverlay: Finishing gesture")
        if self.overlay is not None:
            ii, ij, iw, ih, fi, fj, fw, fh, t = self.overlay.close()
            self.overlay = None

            if ii != fi or ij != fj:
                self._target_view_pos = (ii, ij, fi, fj, time.time(), time.time() + t)
            if iw != fw or iw != fw:
                self._target_view_size = (iw, ih, fw, fh, time.time(), time.time() + t)


        if not self.layout.modifiers & self.layout.mod:
            logging.debug("MoveResizeOverlay: Requesting close after gesture finish")
            self.close()

    def on_motion(self, time_msec, delta_x, delta_y):
        return False

    def on_axis(self, time_msec, source, orientation, delta, delta_discrete):
        return False

    def on_key(self, time_msec, keycode, state, keysyms):
        if state != PYWM_PRESSED and self.layout.mod_sym in keysyms:
            if self.overlay is None:
                logging.debug("MoveResizeOverlay: Requesting close after Mod release")
                self.close()

    def on_modifiers(self, modifiers):
        return False

    def close(self):
        if self.overlay is not None:
            self.overlay.close()
        self._wants_close = True

    def pre_destroy(self):
        self._running = False

    def _exit_transition(self):
        self.layout.update_cursor(True)
        try:
            # Clean up any possible mishaps - should not be necessary
            view_state = self.layout.state.get_view_state(self.view)
            i = round(view_state.i)
            j = round(view_state.j)
            w = round(view_state.w)
            h = round(view_state.h)

            logging.debug("MoveResizeOverlay: Exiting with animation %d, %d, %d, %d -> %d, %d, %d, %d",
                          view_state.i, view_state.j, view_state.w, view_state.h, i, j, w, h)

            return self.layout.state.replacing_view_state(
                self.view,
                i=i, j=j, w=w, h=h,
                scale_origin=(None, None), move_origin=(None, None)), .3
        except Exception:
            logging.warn("Unexpected: Error accessing view %s state", self.view)
            return None, 0
