"""Sigrok decoder helper utilities."""

def bitpack(bits):
    """Pack bits into a value (Least Significant Bit first).
    
    Args:
        bits: List of bit values (0 or 1)
    
    Returns:
        Packed integer value
    """
    value = 0
    for i, bit in enumerate(bits):
        if isinstance(bit, (list, tuple)):
            bit_value = bit[0]
        else:
            bit_value = bit
        value |= ((1 if bit_value else 0) << i)
    return value

def bitpack_msb(bits, start_pos=0):
    """Pack bits into a value (Most Significant Bit first).
    
    Args:
        bits: List of [value, ss, es] where value is 0 or 1
        start_pos: Starting position in the bits list
    
    Returns:
        Packed integer value
    """
    value = 0
    for i, bit in enumerate(bits[start_pos:]):
        if isinstance(bit, (list, tuple)):
            bit_value = bit[0]
        else:
            bit_value = bit
        value = (value << 1) | (1 if bit_value else 0)
    return value
