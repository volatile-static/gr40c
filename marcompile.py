#!/usr/bin/env python3
# Basic CSV -> machine code compiler for marga

import numpy as np
import warnings
from marmachine import *
try:
    from local_config import grad_board
except ModuleNotFoundError:
    grad_board = "gpa-fhdo"

import pdb
st = pdb.set_trace

grad_data_bufs = (1, 2)

max_removed_instructions = 1000

def debug_print(*args, **kwargs):
    # print(*args, **kwargs)
    pass

def col2buf(col_idx, value):
    """ Returns a tuple of (buffer indices), (values), (value masks)
    A value masks specifies which bits are actually relevant on the output.
    Can accept arrays of values."""
    if col_idx in (1, 2, 3, 4): # TX
        buf_idx = col_idx + 4, # TX0_I, TX0_Q, TX1_I, TX1_Q
        val = value,
        mask = 0xffff,
    elif col_idx in (5, 6, 7, 8, 9, 10, 11, 12): # grad
        # Only encode value and channel into words here.  Precise
        # timing and broadcast logic will be handled at the next stage
        if grad_board == "gpa-fhdo":
            if col_idx in (9, 10, 11, 12):
                raise RuntimeError("GPA-FHDO is selected, but you are trying to control OCRA1")
            grad_chan = col_idx - 5
            val_full = value | 0x80000 | ( grad_chan << 16 ) | (grad_chan << 25)
        elif grad_board == "ocra1" or grad_board == "ocra40":
            if col_idx in (5, 6, 7, 8):
                raise RuntimeError("OCRA1 is selected, but you are trying to control GPA-FHDO")
            grad_chan = col_idx - 9
            val_full = value << 2 | 0x00100000 | (grad_chan << 25) | 0x01000000 # always broadcast by default
        else:
            raise ValueError("Unknown grad board")

        buf_idx = 2, 1 # GRAD_MSB, GRAD_LSB
        val = val_full >> 16, val_full & 0xffff
        mask = 0xffff, 0xffff
    elif col_idx in (13, 14): # RX rate
        buf_idx = col_idx - 10, # RX0_RATE, RX1_RATE
        val = value,
        mask = 0xffff,
    elif col_idx in (15, 16): # RX rate valid
        buf_idx = 16, # RX_CTRL
        bit_idx = col_idx - 15
        val = value << (4 + bit_idx),
        mask = 0x1 << (4 + bit_idx),
    elif col_idx in (17, 18): # RX resets, active low
        buf_idx = 16, # RX_CTRL
        bit_idx = col_idx - 17
        val = value << (6 + bit_idx),
        mask = 0x1 << (6 + bit_idx),
    elif col_idx in (19, 20): # RX resets, active low
        buf_idx = 16, # RX_CTRL
        bit_idx = col_idx - 19
        val = value << (8 + bit_idx),
        mask = 0x1 << (8 + bit_idx),
    elif col_idx in (21, 22, 23): # TX/RX gates, external trig
        buf_idx = 15, # GATES_LEDS
        bit_idx = col_idx - 21
        val = value << bit_idx,
        mask = 0x1 << bit_idx,
    elif col_idx == 24: # LEDs
        buf_idx = 15, # GATES_LEDS
        val = value << 8,
        mask = 0xff00,
    elif col_idx in (25, 26, 27): # LO freqs
        lo_lsb_buf = 9 + 2*(col_idx - 25) # 9, 11 or 13
        buf_idx = lo_lsb_buf, lo_lsb_buf + 1 # DDS[0,1,2]_PHASE_LSB, DDS[0,1,2]_PHASE_MSB
        val = value & 0xffff, value >> 16
        mask = 0xffff, 0x7fff
    elif col_idx in (28, 29, 30): # LO phase reset
        lo_msb_buf = 10 + 2*(col_idx - 28) # DDS[0,1,2]_PHASE_MSB
        buf_idx = lo_msb_buf,
        val = value << 15,
        mask = 0x8000,
    elif col_idx in (31, 32): # LO source for RX demodulation
        buf_idx = 16, # RX_CTRL
        bit_idx = (col_idx - 31) * 2
        val = value << bit_idx,
        mask = 0x0003 << bit_idx,
    elif col_idx in (33, 34):  # RX rate
        buf_idx = col_idx - 16, # RX2_RATE, RX3_RATE
        val = value,
        mask = 0xffff,
    elif col_idx in (35, 36): # RX rate valid
        buf_idx = 19, # RX_CTRL
        bit_idx = col_idx - 35
        val = value << (4 + bit_idx),
        mask = 0x1 << (4 + bit_idx),
    elif col_idx in (37, 38): # RX resets, active low
        buf_idx = 19,
        bit_idx = col_idx - 37
        val = value << (6 + bit_idx),
        mask = 0x1 << (6 + bit_idx),
    elif col_idx in (39, 40): # RX en, active low
        buf_idx = 19,
        bit_idx = col_idx - 39
        val = value << (8 + bit_idx),
        mask = 0x1 << (8 + bit_idx),
    elif col_idx in (41, 42):  # LO source for RX demodulation
        buf_idx = 19, # RX_CTRL
        bit_idx = (col_idx - 41) * 2
        val = value << bit_idx,
        mask = 0x0003 << bit_idx,
    elif col_idx in range(43, 43 + 40):
        # Only encode value and channel into words here.  Precise
        # timing and broadcast logic will be handled at the next stage
        if grad_board == "ocra40":
            grad_chan = col_idx - 43
            val_full = value << 2 | 0x00100000 | (grad_chan << 25) | 0x01000000 # always broadcast by default
        else:
            raise ValueError("Unsupported grad board")
        buf_idx = 2, 1 # GRAD_MSB, GRAD_LSB
        val = val_full >> 16, val_full & 0xffff
        mask = 0xffff, 0xffff
    
    return buf_idx, val, mask

def csv2bin(path, quick_start=False, initial_bufs=np.zeros(MARGA_BUFS, dtype=np.uint16), latencies = np.zeros(MARGA_BUFS, dtype=np.int32)):
    """ initial_bufs: starting state of output buffers, to track with instructions
    quick_start: strip out the initial RAM-writing dead time if the CSV was generated by the simulator or similar
    latencies: inherent buffer latencies to take into
    account. Latencies are primarily relevant to the gradients, but
    can be adjusted to suit various other external hardware effects
    like slow RF amps, very long cables etc
    """

    # Input: CSV column, starting from 0 for tx0 i and ending with 21 for leds
    # Output: corresponding buffer index or indices to change

    data = np.loadtxt(path, skiprows=1, delimiter=',', comments='#').astype(np.uint32)
    with open(path, 'r') as csvf:
        cols = csvf.readline().strip().split(',')[1:]

    assert cols[-1] == ' csv_version_0.2', "Wrong CSV format"

    if quick_start:
        # remove dead time in the beginning taken up by simulated memory writes, if the input CSV is generated from the simulator
        # data[1:, 0] = data[1:, 0] - data[1, 0] + latencies.max()
        data[1:, 0] = data[1:, 0] - data[1, 0] + 10

    # Boolean: compare data offset by one row in time
    data_diff = data[:-1,1:] != data[1:,1:]

    changelist = []
    changelist_grad = []

    for k, dd in enumerate(data_diff):
        clocktime = data[k + 1, 0]
        dw = np.where(dd)[0] # indices where data changed
        for col_idx, value in zip(dw + 1, data[k + 1][dw + 1]):
            buf_idces, vals, masks = col2buf(col_idx, value)
            for bi, v, m in zip(buf_idces, vals, masks):
                change = clocktime - latencies[bi], bi, v, m
                if bi in grad_data_bufs:
                    changelist_grad.append(change)
                else:
                    changelist.append(change)

    return cl2bin(changelist, changelist_grad, initial_bufs)

def dict2bin(sd, initial_bufs=np.zeros(MARGA_BUFS, dtype=np.uint16), latencies = np.zeros(MARGA_BUFS, dtype=np.int32)):
    """sd: sequence dictionary, consisting of something in the form of:

     {'tx0_i': ( np.array([100, 102, 304, 506]), np.array([1, 200, 65535, 20000]) ),
      'fhdo_vx': ( np.array([3000, 4500, 5900, 7000]), np.array([1, 2, 55555, 33333]) ),
      'fhdo_vy': ( np.array([10000, 12000, 14000, 16000]), np.array([1, 2, 55555, 33333]) ) }

    etc. Same binary format as in the CSV file.

    latencies: inherent buffer latencies to take into
    account. Latencies are primarily relevant to the gradients, but
    can be adjusted to suit various other external hardware effects
    like slow RF amps, very long cables etc
    """

    col_arr = ['clock cycles', 'tx0_i', 'tx0_q', 'tx1_i', 'tx1_q', 'fhdo_vx', 'fhdo_vy', 'fhdo_vz', 'fhdo_vz2',
               'ocra1_vx', 'ocra1_vy', 'ocra1_vz', 'ocra1_vz2', 'rx0_rate', 'rx1_rate',
               'rx0_rate_valid', 'rx1_rate_valid', 'rx0_rst_n', 'rx1_rst_n', 'rx0_en', 'rx1_en',
               'tx_gate', 'rx_gate', 'trig_out', 'leds',
               'lo0_freq', 'lo1_freq', 'lo2_freq', 'lo0_rst', 'lo1_rst', 'lo2_rst',
               'rx0_lo', 'rx1_lo', 
               'rx2_rate', 'rx3_rate',
               'rx2_rate_valid', 'rx3_rate_valid', 'rx2_rst_n', 'rx3_rst_n', 'rx2_en', 'rx3_en',
               'rx2_lo', 'rx3_lo',
               'ocra40_v0', 'ocra40_v1', 'ocra40_v2', 'ocra40_v3', 'ocra40_v4', 'ocra40_v5', 'ocra40_v6', 'ocra40_v7',
               'ocra40_v8', 'ocra40_v9', 'ocra40_v10', 'ocra40_v11', 'ocra40_v12', 'ocra40_v13', 'ocra40_v14', 'ocra40_v15',
               'ocra40_v16', 'ocra40_v17', 'ocra40_v18', 'ocra40_v19', 'ocra40_v20', 'ocra40_v21', 'ocra40_v22', 'ocra40_v23',
               'ocra40_v24', 'ocra40_v25', 'ocra40_v26', 'ocra40_v27', 'ocra40_v28', 'ocra40_v29', 'ocra40_v30', 'ocra40_v31',
               'ocra40_v32', 'ocra40_v33', 'ocra40_v34', 'ocra40_v35', 'ocra40_v36', 'ocra40_v37', 'ocra40_v38', 'ocra40_v39',
               ] # TODO: these two rows aren't yet in the CSV and thus aren't tested by test_marga_model.py

    changelist = []
    changelist_grad = []

    for k, vals in sd.items(): # iterate over dictionary keys
        col_idx = col_arr.index(k)
        changelist_grad_local = []
        buf_idces, values, masks = col2buf(col_idx, vals[1]) # single element or array of values
        t_corr = vals[0] - latencies[buf_idces[0]]
        for bi, vv, m in zip(buf_idces, values, masks):
            for t, v in zip(t_corr, vv):
                change = t, bi, v, m
                if bi in grad_data_bufs:
                    changelist_grad_local.append(change)
                else:
                    changelist.append(change)

        # needed to keep coupled LSB/MSB pairs together in case
        # multiple events occur on different channels simultaneously
        if len(changelist_grad_local) != 0:
            changelist_grad_local.sort(key=lambda change: change[0])
            changelist_grad += changelist_grad_local

    return cl2bin(changelist, changelist_grad, initial_bufs)

def cl2bin(changelist, changelist_grad,
           initial_bufs=np.zeros(MARGA_BUFS, dtype=np.uint16)):

    """Central compilation function; accept in two changelists,
    changelist for all the direct-buffer outputs (TX, most configurable
    parameters, etc) and the other, changelist_grad, for the outputs used
    to control hardware with non-trivial internal timing behaviour
    (currently only the gradient boards). Also accepts non-default initial
    values to program the buffers to."""

    # Process the grad changelist, depending on what GPA is being used etc
    # Sort in pairs of changes, because otherwise channels can get mixed up
    changelist_grad_paired = [ [k, m] for k, m in zip(changelist_grad[::2], changelist_grad[1::2]) ]
    sortfn = lambda change: change[0]
    # changelist_grad.sort(key=sortfn) # sort by time
    sortfn_paired = lambda change: change[0][0]
    changelist_grad_paired.sort(key=sortfn_paired) # sort by time
    changelist_grad = [k for sl in changelist_grad_paired for k in sl] # https://stackabuse.com/python-how-to-flatten-list-of-lists/

    t_last = [0, 0] # no updates have previously happened; [LSB, MSB]
    spi_div = (initial_bufs[0] & 0xfc) >> 2
    changelist_grad_shifted = []
    num_chgs = [0, 0] # [LSB, MSB]
    grad_vals = [initial_bufs[1], initial_bufs[2]] # [LSB, MSB] current output data
    grad_vals_old = [0, 0] # [LSB, MSB] previous output data

    for c in changelist_grad:
        t = c[0]
        debug_print("t: ", t, " t_last: ", t_last, "num_chgs: ", num_chgs, " c: ", c)
        idx = c[1] - 1 # 0 for LSB, 1 for MSB
        msb = idx == 1
        data = c[2]
        # if data == grad_vals[idx]: # no actual change to buffer output
        #     continue # skip this change
        # else:
        #     grad_vals_old[idx] = grad_vals[idx]
        #     grad_vals[idx] = data # update the last known buffer value

        if t == t_last[idx]:
            num_chgs[idx] += 1
            # assume the changes in changelist_grad are paired with LSBs/MSBs matching each other's grad channels stored sequentially,
            # and that for each event, the MSB update is first
            if grad_board == "ocra1" or grad_board == "ocra40": # simultaneous with another grad update
                if msb:
                    if num_chgs[1]: # MSB buffer and not the first grad event on this timestep
                        # turn broadcast off if this isn't the first grad event on this timestep
                        data = data & ~0x0100
                        # return LSB back to old values, since this one is now done in the past
                        grad_vals[:] = grad_vals_old # revert the last known buffer values
                # else:
                #     if data == grad_vals[idx]: # no actual change to buffer output compared to earlier LSB at this timestep
                #         continue # skip this change

                # move non-broadcast events back in time, so that synchronisation will be done in ocra1_iface core
                changelist_grad_shifted.append( (c[0]-num_chgs[idx], c[1], data, c[3]) )
                num_chgs[idx] += 1
            elif grad_board == "gpa-fhdo":
                # don't do anything; currently will cause an error
                # later since multiple events can't happen at the same
                # time for GPA-FHDO
                changelist_grad_shifted.append(c)     
        else:
            if t - t_last[idx] < 24 * (1 + spi_div) + 2: #
                warnings.warn("Gradient updates are too frequent for selected SPI divider. Missed samples are likely!", MarGradWarning)

            # if data == grad_vals[idx]: # no actual change to buffer output
            #     continue # skip this change

            t_last[idx] = t
            grad_vals[idx] = data # update the last known buffer value
            changelist_grad_shifted.append(c)
            num_chgs = [0, 0]

    changelist += changelist_grad_shifted
    changelist.sort(key=sortfn) # sort by time

    # Track removed instruction events, but only warn when the number exceeds a minimum
    removed_instruction_warnings = []

    # Process and combine the change list into discrete sets of operations at each time, i.e. an output list
    def cl2ol(changelist):
        current_bufs = initial_bufs.copy()
        current_time = changelist[0][0]
        unique_times = []
        unique_changes = []
        change_masks = np.zeros(MARGA_BUFS, dtype=np.uint16)
        changed = np.zeros(MARGA_BUFS, dtype=bool)

        def close_timestep(time):
            ch_idces = np.where(changed)[0]
            # buf_time_offsets = np.zeros(MARGA_BUFS, dtype=int32)
            buf_time_offsets = 0
            unique_changes.append( [time, ch_idces, current_bufs[ch_idces], buf_time_offsets] )
            change_masks[:] = np.zeros(MARGA_BUFS, dtype=np.uint16)
            changed[:] = np.zeros(MARGA_BUFS, dtype=bool)

        for time, buf, val, mask in changelist:
            if time != current_time:
                close_timestep(current_time)
                current_time = time
            buf_diff = (current_bufs[buf] ^ val) & mask
            assert buf_diff & change_masks[buf] == 0, "Tried to set a buffer to two values at once"
            if buf_diff == 0:
                if buf not in (1, 2):
                    # gradient buffers will have unneeded instructions
                    # all the time, so not worth warning the user for
                    # those
                    removed_instruction_warnings.append( "Instruction at tick {:d}, buffer {:d}, value 0x{:04x}, mask 0x{:04x} will have no effect. Skipping...".format(time, buf, val, mask) )
                continue
            val_masked = val & mask
            old_val_unmasked = current_bufs[buf] & ~mask
            new_val = old_val_unmasked | val_masked
            change_masks[buf] |= mask
            current_bufs[buf] = new_val
            changed[buf] = True

        close_timestep(current_time)

        return unique_changes

    changes = cl2ol(changelist)

    # warn about all the removed instructions if there are more than a maximum number
    if len(removed_instruction_warnings) > max_removed_instructions:
        for riw in removed_instruction_warnings:
            warnings.warn(riw, MarRemovedInstructionWarning)
        warnings.warn("NOTE: Fewer than {:d} removed-instruction warnings will not be printed -- keep this in mind when searching for the root cause.".format(max_removed_instructions))

    # Process time offsets
    for ch, ch_prev in zip( reversed(changes[1:]), reversed(changes[:-1]) ):
        # does the current timestep need to output more data than can
        # fit into the time gap since the previous timestep?
        timestep = ch[0] - ch_prev[0]
        timediff = ch[1].size - timestep
        # if timestep < ch[1].size: # not enough time

        if timediff > 0:
            ch_prev[0] -= timediff # move prev. event into the past
            ch_prev[3] = timediff # make prev. event's buffers output in its future

    # convert to differential timesteps
    last_time = 0
    for ch in changes:
        ch0 = ch[0]
        ch[0] = ch0 - last_time
        last_time = ch0

    # Interpretation of each element of changes list:
    # [time when all instructions for this change will have completed,
    #  buffers that need to be changed,
    #  values to set the buffers to,
    #  the delay until the buffers will output their values]

    ### Write out instructions

    # Write out initial buffer values
    bdata = []
    addr = 0
    states = initial_bufs
    # reversed order, so that grad board is enabled last of all (to avoid spurious initial transfer)
    for k, ib in enumerate(reversed(initial_bufs)):
        bdata.append(instb(MARGA_BUFS-1-k, k, ib))

    last_buf_time_left = np.zeros(MARGA_BUFS, dtype=np.int32)
    buf_time_left = np.zeros(MARGA_BUFS, dtype=np.int32)
    # buf_empty_time = np.zeros(MARGA_BUFS, dtype=np.int32)
    debug_print("changes:")
    for k in changes:
        debug_print(k)

    for event in changes:
        b_instrs = event[1].size
        dtime = event[0]

        # soak up any extra time which is in excess of what the instructions need to execute synchronously
        excess_dtime = dtime - b_instrs
        excess_dtime_tmp = excess_dtime
        while excess_dtime_tmp > 2: # delay of 3 or more cycles needed
            wait_time = min(excess_dtime_tmp, COUNTER_MAX + 3) # delay for the time instruction
            bdata.append(insta(IWAIT, wait_time - 3))
            excess_dtime_tmp -= wait_time
            debug_print("i wait ", wait_time - 3)
        if excess_dtime_tmp: # final delay of 1 or 2 cycles
            for k in range(dtime - b_instrs):
                debug_print("i nop")
                bdata.append(insta(INOP, 0))

        # time left after delays from nops or waits
        # dtime_eff could be increased later with a more advanced
        # compiler, to make the buffers bear more of the internal
        # delays
        # dtime_eff = b_instrs

        # count down the times until each channel buffer will be empty
        buf_time_left -= excess_dtime
        buf_time_left[buf_time_left < 0] = 0
        this_time_offset = event[3]
        debug_print("--- dtime {:2d}, this_time_offset: {:2d}, b_instrs: {:2d}, lbtl: ".format(dtime, this_time_offset, b_instrs), last_buf_time_left[5:9])
        for m, (ind, dat) in enumerate(zip(event[1], event[2])):
            execution_delay = b_instrs - m - 1 #+ time - 2
            btli = buf_time_left[ind]
            buf_empty = btli <= m # or <= m, need to check
            if buf_empty: # buffer empty for this instruction; need an appropriate delay only for sync
                # (check against m since with successive cycles, remaining buffers will empty out)
                extra_delay = execution_delay + this_time_offset
                buf_time_left[ind] = this_time_offset + b_instrs
            else:
                # buffer already not empty on this cycle
                extra_delay = this_time_offset - btli + b_instrs - 1
                buf_time_left[ind] += extra_delay + 1

            debug_print("bti={:d} btli={:d} m={:d} empty={:d} edel={:d} instb i {:d} del {:d} dat {:d}".format(
                buf_time_left[ind], btli, m, buf_empty, execution_delay, ind, extra_delay, dat))
            bdata.append(instb(ind, extra_delay, dat))

        buf_time_left -= b_instrs # take into account execution time of this timestep

    # Finish sequence
    bdata.append(insta(IFINISH, 0))
    return bdata

CIC_SLOWEST_RATE_NEAREST_POW2 = 1 << np.ceil(np.log2(CIC_SLOWEST_RATE)).astype(int)

def cic_words(rate, set_cic_shift=False):
    # Calculate the data words to transfer to the CIC for a given rate

    # FLoating-point calculation
    assert np.all( (CIC_FASTEST_RATE <= rate) & (rate <= CIC_SLOWEST_RATE) ), "RX rate outside valid range"
    gain_factor_log2 = CIC_STAGES * np.log2( CIC_SLOWEST_RATE_NEAREST_POW2 / rate )
    gain_shift = np.int32(gain_factor_log2) # rounded down
    a = (1 << CIC_RATE_DATAWIDTH) | gain_shift
    b = (0 << CIC_RATE_DATAWIDTH) | rate
    excess_factor = 2**(gain_factor_log2 - gain_shift)
    # b = (2 << rate_datawidth) | int(factor_excess * (1 << (rate_datawidth - 1)) ) # TODO: tell Benjamin about assumed 1 - i.e. save a bit for multiplicand by assuming it's between 1 and 2

    if set_cic_shift:
        return (a, b), excess_factor
    else:
        return (b,), excess_factor

if __name__ == "__main__":
    csv2bin("/tmp/marga.csv")
